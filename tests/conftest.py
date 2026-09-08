"""Config de tests.

Todo corre contra `fixtures/llm/` con FAKE_LLM: con 50 llamadas diarias no se puede
iterar la suite contra la API real. El renderer, el compilador y el gate anti-invento
se prueban enteros sin gastar cupo.
"""

import pytest
from django.contrib.auth.models import User

from apps.llm import store
from config.celery import app as celery_app


@pytest.fixture(autouse=True)
def _local_pipeline(settings, tmp_path):
    # Se fija sobre la app de Celery, no solo en settings: la app ya está configurada
    # cuando pytest-django carga esto, y un cambio en settings no la alcanza.
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.FAKE_LLM = True
    settings.OPENROUTER_API_KEY = "test-key-no-se-usa"
    settings.MEDIA_ROOT = tmp_path / "media"
    # Store en memoria: si no, los tests comparten cooldowns y contador de cupo con el
    # Redis de desarrollo y se contaminan entre sí.
    store.use_memory(True)
    yield
    store.use_memory(False)


@pytest.fixture
def user(db):
    return User.objects.create_user("ana", "ana@example.com", "clave-larga-123")


@pytest.fixture
def job(db, user):
    from apps.jobs.models import Job

    return Job.objects.create(
        user=user,
        target_role="Senior Backend Engineer",
        job_description="Buscamos backend con Python, Django, PostgreSQL y AWS. "
                        "Requisitos: 5 años de experiencia, APIs REST, Kubernetes.",
        language="es",
        cv_text="Ana Gómez Ríos, desarrolladora backend con experiencia en Python.",
        cv_hash="hash-de-prueba",
    )
