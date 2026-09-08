"""Pipeline de Celery.

Es una cadena de tres tasks en vez de una sola con pasos, porque Celery reintenta la
task individual y no la cadena: un retry de `tailor` nunca vuelve a disparar la llamada
LLM de `parse_cv`. Cada task deja su checkpoint en el Job, así que `resume_job` puede
armar una cadena solo con los eslabones que faltan.

La política de reintentos está calibrada contra el cupo, no contra la latencia. Un
`bad_json` no se reintenta: `complete_json` ya rotó por varios modelos y volver a
intentar quemaría otras tantas llamadas de las 50 del día. Solo se reintenta lo que el
tiempo arregla solo, que es quedarse sin modelos sanos o un throttling del proveedor.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

from celery import Task, chain, shared_task
from django.conf import settings
from django.core.files.base import ContentFile

from apps.jobs.models import Artifact, Job
from apps.llm import prompts, quota, registry, schemas
from apps.llm.client import LlmError, complete_json
from apps.llm.models import CvParse
from apps.resume import merge, render
from apps.resume.compile import LatexError, compile_fitted

log = logging.getLogger(__name__)

PARSE_MAX_TOKENS = 6000
TAILOR_MAX_TOKENS = 3500
# Fallos que el paso del tiempo arregla; el resto falla rápido para no gastar cupo.
RETRYABLE_KINDS = {"no_model", "rate_limit", "server_error", "timeout", "connect_error"}


class JobTask(Task):
    """Marca el Job como fallado si la task muere. Sin esto se queda en curso para siempre."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):  # noqa: ANN001
        job_id = kwargs.get("job_id") or (args[0] if args else None)
        if not job_id:
            return
        job = Job.objects.filter(pk=job_id).first()
        if job is None or job.is_terminal:
            return
        status = (
            Job.Status.QUOTA_EXHAUSTED
            if isinstance(exc, LlmError) and exc.kind == "quota_exhausted"
            else Job.Status.FAILED
        )
        job.mark(status, error=str(exc)[:2000])
        log.error("Job %s falló en %s: %s", job_id, self.name, exc)


def _handle_llm_error(task: Task, job: Job, exc: LlmError):
    """Reintenta solo lo que se arregla esperando; el resto sube y mata el job."""
    if exc.kind in {"quota_exhausted", "budget_exhausted"}:
        job.mark(Job.Status.QUOTA_EXHAUSTED, error=str(exc))
        raise exc
    if exc.kind in RETRYABLE_KINDS and task.request.retries < task.max_retries:
        # wait_hint solo tiene sentido con el pool de OpenRouter; con Bedrock la
        # cadena es fija y alcanza con un backoff simple.
        hint = registry.wait_hint() if settings.LLM_PROVIDER == "openrouter" else 60
        delay = max(60, min(hint, 600)) * (task.request.retries + 1)
        log.warning("Job %s: %s, reintento en %ds", job.id, exc.kind, delay)
        raise task.retry(exc=exc, countdown=delay)
    raise exc


def _record(job: Job, result) -> None:
    if result.model_id and result.model_id not in job.models_used:
        job.models_used = [*job.models_used, result.model_id]
    if result.notes:
        job.notes = [*job.notes, *result.notes]


# --- paso 1: parse del CV ---------------------------------------------------


@shared_task(bind=True, base=JobTask, max_retries=2, name="jobs.parse_cv")
def parse_cv(self, job_id: str) -> str:
    job = Job.objects.get(pk=job_id)
    job.mark(Job.Status.PARSING)

    if job.parsed_cv:  # reanudación: el checkpoint ya existe
        return job_id

    cached = CvParse.objects.filter(cv_hash=job.cv_hash).first()
    if cached:
        log.info("Job %s reusa el parse del CV %s (0 llamadas)", job_id, job.cv_hash[:12])
        job.parsed_cv = merge.assign_ids(cached.data)
        job.save(update_fields=["parsed_cv", "updated_at"])
        return job_id

    try:
        result = complete_json(
            purpose="parse",
            system=prompts.PARSE_SYSTEM,
            user=prompts.parse_user_prompt(job.cv_text),
            schema=schemas.PARSED_CV_SCHEMA,
            schema_name="parsed_cv",
            model_cls=schemas.ParsedCv,
            max_tokens=PARSE_MAX_TOKENS,
            example=prompts.PARSE_EXAMPLE,
        )
    except LlmError as exc:
        _handle_llm_error(self, job, exc)
        raise

    parsed = merge.assign_ids(result.data)
    if not parsed.get("experience") and not parsed.get("education"):
        raise ValueError(
            "El modelo no encontró experiencia ni formación en el CV. "
            "Revisá que el texto extraído sea correcto."
        )

    CvParse.objects.update_or_create(
        cv_hash=job.cv_hash, defaults={"data": parsed, "model_id": result.model_id}
    )
    job.parsed_cv = parsed
    _record(job, result)
    job.save(update_fields=["parsed_cv", "models_used", "notes", "updated_at"])
    return job_id


# --- paso 2: adaptación a la oferta -----------------------------------------


@shared_task(bind=True, base=JobTask, max_retries=2, name="jobs.tailor")
def tailor(self, job_id: str) -> str:
    job = Job.objects.get(pk=job_id)
    job.mark(Job.Status.TAILORING)

    if job.final_cv:
        return job_id

    try:
        result = complete_json(
            purpose="tailor",
            system=prompts.TAILOR_SYSTEM,
            user=prompts.tailor_user_prompt(
                parsed=job.parsed_cv,
                target_role=job.target_role,
                job_description=job.job_description,
                language=job.language,
                include_summary=job.include_summary,
            ),
            schema=schemas.TAILOR_PATCH_SCHEMA,
            schema_name="tailor_patch",
            model_cls=schemas.TailorPatch,
            max_tokens=TAILOR_MAX_TOKENS,
            example=prompts.TAILOR_EXAMPLE,
        )
    except LlmError as exc:
        _handle_llm_error(self, job, exc)
        raise

    patch = result.data
    final_cv, warnings = merge.merge_patch(
        job.parsed_cv, patch, include_summary=job.include_summary
    )

    job.tailor_patch = patch
    job.final_cv = final_cv
    job.warnings = warnings
    job.match_report = merge.build_match_report(patch, warnings)
    job.linkedin_pack = merge.build_linkedin_pack(patch)
    _record(job, result)
    job.save(update_fields=["tailor_patch", "final_cv", "warnings", "match_report",
                            "linkedin_pack", "models_used", "notes", "updated_at"])
    return job_id


# --- paso 3: render y compilación (sin LLM) ---------------------------------


def _save_artifact(job: Job, kind: str, filename: str, payload: bytes) -> None:
    existing = job.artifacts.filter(kind=kind).first()
    if existing:
        existing.file.delete(save=False)
        existing.delete()
    artifact = Artifact(job=job, kind=kind)
    artifact.file.save(filename, ContentFile(payload), save=False)
    artifact.save()


def _bundle(tex: str, pdf_bytes: bytes, data: dict) -> bytes:
    """ZIP listo para subir a Overleaf: arrastrás el zip y compila."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("cv.tex", tex)
        archive.writestr("cv.pdf", pdf_bytes)
        archive.writestr("cv.json", json.dumps(data, ensure_ascii=False, indent=2))
        archive.writestr(
            "LEEME.txt",
            "CV generado con cv-modista.\n\n"
            "cv.tex   fuente LaTeX. Compila sin cambios con pdflatex, xelatex y lualatex.\n"
            "cv.pdf   el mismo CV ya compilado.\n"
            "cv.json  los datos estructurados, por si querés re-renderizar.\n\n"
            "Para editarlo en Overleaf: New Project > Upload Project > subí este zip.\n",
        )
    return buffer.getvalue()


@shared_task(bind=True, base=JobTask, max_retries=1, name="jobs.render_cv")
def render_cv(self, job_id: str) -> str:
    job = Job.objects.get(pk=job_id)
    job.mark(Job.Status.RENDERING)

    language = job.cv_language
    workdir = Path(tempfile.mkdtemp(prefix=f"cvmodista-{job.pk}-"))
    try:
        result = compile_fitted(
            job.final_cv, lambda data: render.render_tex(data, language=language), workdir
        )
        pdf_bytes = result["pdf_path"].read_bytes()
        _save_artifact(job, Artifact.Kind.TEX, "cv.tex", result["tex"].encode("utf-8"))
        _save_artifact(job, Artifact.Kind.PDF, "cv.pdf", pdf_bytes)
        _save_artifact(
            job, Artifact.Kind.JSON, "cv.json",
            json.dumps(job.final_cv, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        _save_artifact(job, Artifact.Kind.ZIP, "cv.zip",
                       _bundle(result["tex"], pdf_bytes, job.final_cv))
    except LatexError as exc:
        job.mark(Job.Status.FAILED, error=f"LaTeX: {exc}")
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    job.pages = result["pages"]
    job.trimmed_bullets = result["trimmed"]
    job.ats_missing = result["ats_missing"]
    job.status = Job.Status.DONE
    job.error = ""
    job.save(update_fields=["pages", "trimmed_bullets", "ats_missing", "status", "error",
                            "final_cv", "updated_at"])
    return job_id


# --- orquestación -----------------------------------------------------------


def enqueue(job: Job) -> None:
    chain(parse_cv.s(str(job.pk)), tailor.s(), render_cv.s()).apply_async()


def resume_job(job: Job) -> None:
    """Rearma la cadena solo con los pasos que faltan. Un checkpoint hecho no se repite."""
    steps = []
    if not job.parsed_cv:
        steps.append(parse_cv.s(str(job.pk)))
    if not job.final_cv:
        steps.append(tailor.s(str(job.pk)) if not steps else tailor.s())
    steps.append(render_cv.s(str(job.pk)) if not steps else render_cv.s())

    job.mark(Job.Status.PENDING, error="")
    chain(*steps).apply_async()


@shared_task(name="jobs.sync_models")
def sync_models() -> int:
    """Beat: la lista de modelos :free rota seguido. Con Bedrock no hay nada que
    sincronizar, la cadena de modelos es fija."""
    if settings.LLM_PROVIDER != "openrouter":
        return 0
    pool = registry.sync_pool(force=True)
    quota.reconcile()
    return len(pool)
