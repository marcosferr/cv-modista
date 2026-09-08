from django.db import models


class LlmCall(models.Model):
    """Caché e historial de llamadas al LLM.

    La clave **no incluye el model_id** a propósito: el objetivo del caché es no
    quemar cupo, y si rotar de modelo invalidara el caché cada rotación costaría una
    llamada. Como solo se persiste después de que el schema valida, cualquier entrada
    cacheada es buena venga del modelo que venga.
    """

    PURPOSE_CHOICES = [
        ("parse", "Parse del CV"),
        ("tailor", "Adaptación a la oferta"),
        ("repair", "Reparación de JSON"),
    ]

    cache_key = models.CharField(max_length=64, unique=True, db_index=True)
    purpose = models.CharField(max_length=16, choices=PURPOSE_CHOICES)
    prompt_version = models.CharField(max_length=16)
    provider = models.CharField(max_length=20, blank=True)
    model_id = models.CharField(max_length=200)
    data = models.JSONField(default=dict)
    raw_response = models.TextField(blank=True)
    finish_reason = models.CharField(max_length=32, blank=True)
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    cost_usd = models.FloatField(default=0.0)
    latency_ms = models.PositiveIntegerField(default=0)
    attempts = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.purpose} via {self.model_id}"


class CvParse(models.Model):
    """Parse del CV cacheado por hash del texto, compartido entre jobs.

    Es el multiplicador de cupo del sistema: el parse no depende de la oferta, así que
    postularse a diez puestos con el mismo CV cuesta una sola llamada de parse.
    """

    cv_hash = models.CharField(max_length=64, unique=True, db_index=True)
    data = models.JSONField()
    model_id = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"CvParse {self.cv_hash[:12]}"
