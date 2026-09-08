from __future__ import annotations

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.jobs import tasks, uploads
from apps.jobs.forms import FinalCvForm, JobForm, SignupForm
from apps.jobs.models import Artifact, Job
from apps.llm import quota, registry

log = logging.getLogger(__name__)


def _user_jobs_today(user) -> int:
    since = timezone.now() - timedelta(hours=24)
    return Job.objects.filter(user=user, created_at__gte=since).count()


def _quota_context(user) -> dict:
    status = quota.status()
    used = _user_jobs_today(user)
    limit = settings.JOBS_PER_USER_PER_DAY
    return {
        "quota": status,
        "user_used": used,
        "user_limit": limit,
        "user_blocked": used >= limit,
        "blocked": status["exhausted"] or used >= limit,
    }


def signup(request):
    if request.user.is_authenticated:
        return redirect("job_create")
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.save())
        return redirect("job_create")
    return render(request, "jobs/signup.html", {"form": form})


@login_required
def job_create(request):
    context = _quota_context(request.user)
    form = JobForm(request.POST or None, request.FILES or None, user=request.user)

    if request.method == "POST":
        if context["blocked"]:
            reason = (
                f"Ya lanzaste {context['user_used']} CVs en las últimas 24 h "
                f"(tope: {context['user_limit']})."
                if context["user_blocked"]
                else f"El cupo diario de OpenRouter está agotado. Se libera en "
                     f"{context['quota']['reset_hours']} h."
            )
            messages.error(request, reason)
        elif form.is_valid():
            job = form.save(commit=False)
            job.user = request.user
            form.save()
            tasks.enqueue(job)
            return redirect("job_detail", pk=job.pk)

    return render(request, "jobs/create.html", {
        "form": form, "s3_enabled": uploads.enabled(), **context
    })


@login_required
def job_list(request):
    return render(request, "jobs/list.html", {
        "jobs": Job.objects.filter(user=request.user)[:50],
        **_quota_context(request.user),
    })


@login_required
def job_detail(request, pk):
    job = get_object_or_404(Job, pk=pk, user=request.user)
    return render(request, "jobs/detail.html", {"job": job, "pool": registry.describe_pool()})


@login_required
def job_status(request, pk):
    """Endpoint de polling. En estado terminal el front corta solo."""
    job = get_object_or_404(Job, pk=pk, user=request.user)
    response = JsonResponse({
        "status": job.status,
        "label": job.get_status_display(),
        "progress": job.progress,
        "terminal": job.is_terminal,
        "error": job.error,
        "warnings": len(job.warnings or []),
        "pages": job.pages,
    })
    response["Cache-Control"] = "no-store"
    return response


@login_required
def job_review(request, pk):
    """Diff de bullets, avisos de cifras inventadas y editor del JSON final.

    Guardar acá re-renderiza el PDF sin gastar ni una llamada al LLM.
    """
    job = get_object_or_404(Job, pk=pk, user=request.user)
    if not job.final_cv:
        messages.error(request, "Este job todavía no tiene un CV para revisar.")
        return redirect("job_detail", pk=job.pk)

    if request.method == "POST":
        form = FinalCvForm(request.POST)
        if form.is_valid():
            job.final_cv = form.cleaned_data["payload"]
            job.status = Job.Status.PENDING
            job.save(update_fields=["final_cv", "status", "updated_at"])
            tasks.render_cv.delay(str(job.pk))
            messages.success(request, "Recompilando con tus cambios. No gasta cupo de LLM.")
            return redirect("job_detail", pk=job.pk)
    else:
        form = FinalCvForm(
            initial={"payload": json.dumps(job.final_cv, ensure_ascii=False, indent=2)}
        )

    return render(request, "jobs/review.html", {
        "job": job, "form": form, "diff": _build_diff(job)
    })


def _build_diff(job: Job) -> list[dict]:
    """Bullets originales contra los reescritos, con las cifras marcadas resaltadas."""
    flagged = {w.get("text") for w in job.flagged_numbers}
    rows = []
    for entry in job.final_cv.get("experience") or []:
        original = entry.get("original_bullets") or []
        final = entry.get("bullets") or []
        rows.append({
            "label": f"{entry.get('organization', '')} — {entry.get('title', '')}",
            "pairs": [
                {"before": original[i] if i < len(original) else "",
                 "after": text,
                 "flagged": text in flagged}
                for i, text in enumerate(final)
            ],
            "changed": original != final,
        })
    return rows


@login_required
@require_POST
def job_retry(request, pk):
    job = get_object_or_404(Job, pk=pk, user=request.user)
    tasks.resume_job(job)
    messages.success(request, "Reintentando desde el último paso completado.")
    return redirect("job_detail", pk=job.pk)


@login_required
def job_download(request, pk, kind):
    job = get_object_or_404(Job, pk=pk, user=request.user)
    if kind not in Artifact.Kind.values:
        raise Http404
    artifact = job.artifact(kind)
    if artifact is None:
        raise Http404("Ese archivo todavía no existe.")

    filename = {"tex": "cv.tex", "pdf": "cv.pdf", "json": "cv.json", "zip": "cv.zip"}[kind]
    # El PDF se muestra inline; el resto se descarga.
    return FileResponse(artifact.file.open("rb"), as_attachment=(kind != "pdf"),
                        filename=f"{job.target_role[:40].strip() or 'cv'}-{filename}")


@login_required
@require_POST
def presign_upload(request):
    """Firma una subida directa a S3 para que el archivo no pase por gunicorn."""
    try:
        filename = json.loads(request.body).get("filename", "")
    except ValueError:
        return HttpResponseBadRequest("JSON inválido")
    try:
        return JsonResponse(uploads.presign(request.user.id, filename))
    except uploads.UploadError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
