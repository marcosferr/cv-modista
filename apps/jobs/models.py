from __future__ import annotations

import uuid
from pathlib import Path

from django.conf import settings
from django.db import models


def artifact_path(instance: Artifact, filename: str) -> str:
    return f"jobs/{instance.job_id}/{filename}"


def upload_path(instance: Job, filename: str) -> str:
    return f"jobs/{instance.id}/original/{Path(filename).name}"


class Job(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "En cola"
        PARSING = "PARSING", "Leyendo el CV"
        TAILORING = "TAILORING", "Adaptando a la oferta"
        RENDERING = "RENDERING", "Compilando el PDF"
        DONE = "DONE", "Listo"
        FAILED = "FAILED", "Falló"
        QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED", "Sin cupo diario"

    TERMINAL = {Status.DONE, Status.FAILED, Status.QUOTA_EXHAUSTED}
    # Progreso aproximado por estado, para la barra de la UI.
    PROGRESS = {
        Status.PENDING: 5, Status.PARSING: 30, Status.TAILORING: 65,
        Status.RENDERING: 88, Status.DONE: 100, Status.FAILED: 100,
        Status.QUOTA_EXHAUSTED: 100,
    }

    class Language(models.TextChoices):
        AUTO = "auto", "Igual que la oferta"
        ES = "es", "Español"
        EN = "en", "Inglés"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="jobs"
    )
    target_role = models.CharField(max_length=200)
    job_description = models.TextField()
    language = models.CharField(max_length=4, choices=Language, default=Language.AUTO)
    include_summary = models.BooleanField(default=False)

    cv_source_text = models.TextField(blank=True)
    cv_upload = models.FileField(upload_to=upload_path, blank=True, null=True)
    cv_text = models.TextField(blank=True)
    cv_hash = models.CharField(max_length=64, blank=True, db_index=True)
    extraction_method = models.CharField(max_length=32, blank=True)

    status = models.CharField(max_length=20, choices=Status, default=Status.PENDING)
    error = models.TextField(blank=True)

    # Checkpoints del pipeline: permiten reanudar sin repetir llamadas al LLM.
    parsed_cv = models.JSONField(default=dict, blank=True)
    tailor_patch = models.JSONField(default=dict, blank=True)
    final_cv = models.JSONField(default=dict, blank=True)

    match_report = models.JSONField(default=dict, blank=True)
    linkedin_pack = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    notes = models.JSONField(default=list, blank=True)
    models_used = models.JSONField(default=list, blank=True)

    pages = models.PositiveSmallIntegerField(default=0)
    trimmed_bullets = models.PositiveSmallIntegerField(default=0)
    ats_missing = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.target_role} ({self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL

    @property
    def progress(self) -> int:
        return self.PROGRESS.get(self.status, 0)

    @property
    def cv_language(self) -> str:
        """Idioma de los títulos de sección. En 'auto' se infiere de la oferta."""
        if self.language in {"es", "en"}:
            return self.language
        return detect_language(self.job_description)

    @property
    def flagged_numbers(self) -> list[dict]:
        return [w for w in (self.warnings or []) if w.get("kind") == "invented_number"]

    def artifact(self, kind: str):
        return self.artifacts.filter(kind=kind).first()

    def mark(self, status: str, **fields) -> None:
        self.status = status
        for key, value in fields.items():
            setattr(self, key, value)
        self.save(update_fields=["status", "updated_at", *fields])


class Artifact(models.Model):
    class Kind(models.TextChoices):
        TEX = "tex", "Fuente LaTeX"
        PDF = "pdf", "PDF"
        JSON = "json", "Datos JSON"
        ZIP = "zip", "Paquete completo"

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="artifacts")
    kind = models.CharField(max_length=8, choices=Kind)
    file = models.FileField(upload_to=artifact_path)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("job", "kind")]

    def __str__(self) -> str:
        return f"{self.job_id}:{self.kind}"


# Heurística mínima: cuenta stopwords. Suficiente para elegir el idioma de los
# títulos de sección; el contenido lo escribe el LLM siguiendo la misma instrucción.
_ES_MARKERS = {" de ", " que ", " para ", " con ", " los ", " las ", " experiencia ",
               " requisitos ", " conocimientos ", " años ", " y "}
_EN_MARKERS = {" the ", " and ", " with ", " for ", " you ", " we ", " experience ",
               " requirements ", " skills ", " years "}


def detect_language(text: str) -> str:
    sample = f" {(text or '').lower()} "
    spanish = sum(sample.count(marker) for marker in _ES_MARKERS)
    english = sum(sample.count(marker) for marker in _EN_MARKERS)
    return "en" if english > spanish else "es"
