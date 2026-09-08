"""End-to-end del pipeline contra fixtures. No gasta una sola llamada real."""

import json
import zipfile

import pytest
from django.contrib.auth.models import User

from apps.jobs import tasks
from apps.jobs.models import Job
from apps.llm.models import CvParse, LlmCall
from apps.resume.compile import TectonicCompiler

needs_tectonic = pytest.mark.skipif(
    not TectonicCompiler().available(), reason="tectonic no instalado"
)


@pytest.fixture
def completed(job):
    tasks.enqueue(job)
    job.refresh_from_db()
    return job


@needs_tectonic
def test_el_pipeline_completo_deja_el_job_listo(completed):
    assert completed.status == Job.Status.DONE
    assert completed.error == ""
    assert completed.pages == 1


@needs_tectonic
def test_genera_los_cuatro_artefactos(completed):
    kinds = set(completed.artifacts.values_list("kind", flat=True))
    assert kinds == {"tex", "pdf", "json", "zip"}


@needs_tectonic
def test_el_tex_descargable_es_la_fuente_real(completed):
    tex = completed.artifact("tex").file.read().decode()
    assert tex.startswith("%%") and r"\begin{document}" in tex
    assert "Ana Gómez Ríos" in tex


@needs_tectonic
def test_el_zip_trae_todo_listo_para_overleaf(completed):
    with zipfile.ZipFile(completed.artifact("zip").file) as archive:
        assert set(archive.namelist()) == {"cv.tex", "cv.pdf", "cv.json", "LEEME.txt"}


@needs_tectonic
def test_marca_la_cifra_inventada_del_fixture(completed):
    """El fixture mete un '45%' que no está en el CV original: tiene que aparecer avisado."""
    marcadas = completed.flagged_numbers
    assert len(marcadas) == 1
    assert "45" in marcadas[0]["numbers"]
    assert completed.match_report["flagged_numbers"] == 1


@needs_tectonic
def test_los_hechos_se_copian_verbatim_del_cv_original(completed):
    """El modelo nunca emite empresas ni fechas, así que no puede inventarlas."""
    empresas = [e["organization"] for e in completed.final_cv["experience"]]
    assert empresas == ["Acme & Co. S.A.", "Fintech S.R.L."]
    assert completed.final_cv["experience"][0]["dates"] == "Ene 2021 - Presente"


@needs_tectonic
def test_el_ats_puede_leer_el_pdf_generado(completed):
    assert completed.ats_missing == []


@needs_tectonic
def test_produce_el_pack_de_linkedin(completed):
    pack = completed.linkedin_pack
    assert pack["headline"] and pack["about"] and pack["recruiter_message"]


@needs_tectonic
def test_reusar_el_mismo_cv_no_gasta_otra_llamada_de_parse(completed, user):
    """El parse no depende de la oferta: es el multiplicador de cupo del sistema."""
    assert CvParse.objects.count() == 1
    llamadas_parse = LlmCall.objects.filter(purpose="parse").count()

    otro = Job.objects.create(
        user=user, target_role="Otro puesto", job_description="Otra oferta distinta.",
        language="es", cv_text=completed.cv_text, cv_hash=completed.cv_hash,
    )
    tasks.parse_cv(str(otro.pk))
    otro.refresh_from_db()

    assert otro.parsed_cv["experience"][0]["organization"] == "Acme & Co. S.A."
    assert LlmCall.objects.filter(purpose="parse").count() == llamadas_parse


@needs_tectonic
def test_resume_no_repite_los_pasos_ya_hechos(completed):
    """El botón Reintentar rearma la cadena solo con lo que falta."""
    antes = LlmCall.objects.count()
    tasks.resume_job(completed)
    completed.refresh_from_db()
    assert completed.status == Job.Status.DONE
    assert LlmCall.objects.count() == antes


@needs_tectonic
def test_recompilar_tras_editar_no_llama_al_llm(completed):
    """La función más valiosa: el modelo llega al 80% y vos corregís el resto gratis."""
    antes = LlmCall.objects.count()
    completed.final_cv["contact"]["full_name"] = "Ana Gómez Ríos Corregido"
    completed.save(update_fields=["final_cv"])

    tasks.render_cv(str(completed.pk))
    completed.refresh_from_db()

    assert LlmCall.objects.count() == antes
    assert "Corregido" in completed.artifact("tex").file.read().decode()


def test_el_json_descargable_refleja_el_cv_final(completed):
    if completed.status != Job.Status.DONE:
        pytest.skip("necesita tectonic")
    data = json.loads(completed.artifact("json").file.read())
    assert data["contact"]["full_name"] == completed.final_cv["contact"]["full_name"]


# --- aislamiento entre usuarios ---------------------------------------------


def test_un_usuario_no_ve_los_jobs_de_otro(client, job):
    intruso = User.objects.create_user("otro", "otro@example.com", "clave-larga-456")
    client.force_login(intruso)
    assert client.get(f"/jobs/{job.pk}/").status_code == 404


def test_las_vistas_exigen_login(client, job):
    for url in [f"/jobs/{job.pk}/", "/jobs/", f"/jobs/{job.pk}/download/pdf/", "/"]:
        response = client.get(url)
        assert response.status_code == 302 and "entrar" in response["Location"]


def test_el_listado_solo_muestra_los_propios(client, user, job):
    otro = User.objects.create_user("otro", "otro@example.com", "clave-larga-456")
    Job.objects.create(user=otro, target_role="Ajeno", job_description="x", cv_text="y")
    client.force_login(user)
    contenido = client.get("/jobs/").content.decode()
    assert job.target_role in contenido and "Ajeno" not in contenido


def test_descargar_un_artefacto_inexistente_da_404(client, user, job):
    client.force_login(user)
    assert client.get(f"/jobs/{job.pk}/download/pdf/").status_code == 404
    assert client.get(f"/jobs/{job.pk}/download/exe/").status_code == 404


def test_el_registro_abierto_crea_la_cuenta_y_loguea(client, db):
    response = client.post("/cuenta/registro/", {
        "username": "nueva", "email": "nueva@example.com",
        "password1": "clave-muy-larga-9", "password2": "clave-muy-larga-9",
    })
    assert response.status_code == 302
    assert User.objects.filter(username="nueva").exists()


def test_el_tope_por_usuario_frena_el_envio(client, user, settings, db):
    """Con registro abierto y cupo compartido, un usuario no puede vaciar la cuenta."""
    settings.JOBS_PER_USER_PER_DAY = 1
    Job.objects.create(user=user, target_role="ya hecho", job_description="x", cv_text="y")
    client.force_login(user)
    response = client.post("/", {
        "target_role": "Otro", "job_description": "Buscamos backend Python." * 10,
        "language": "es", "cv_source_text": "Ana Gómez, backend developer. " * 20,
    })
    assert response.status_code == 200
    assert "tope de 1" in response.content.decode()
    assert Job.objects.filter(target_role="Otro").count() == 0
