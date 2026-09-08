"""El gate anti-invento. Los hechos ya están protegidos por diseño; acá se cuidan las cifras."""

from apps.resume.merge import build_match_report, find_invented_numbers, merge_patch

PARSED = {
    "contact": {"full_name": "Ana Gómez"},
    "experience": [
        {"organization": "Acme", "title": "Dev", "dates": "2021-hoy",
         "bullets": ["Reduje la latencia de 800 ms a 210 ms", "Mantuve el módulo legacy"]},
        {"organization": "Vieja SA", "title": "Junior", "dates": "2018-2020",
         "bullets": ["Hice reportes"]},
    ],
    "education": [{"institution": "UNA", "degree": "Ing.", "dates": "2014-2019", "details": []}],
    "extras": [],
    "skills": ["Python", "PostgreSQL", "Docker"],
}


def test_detecta_cifra_inventada():
    inventadas = find_invented_numbers("Mejoré la conversión un 40%",
                                       ["Reduje la latencia de 800 ms a 210 ms"])
    assert inventadas == ["40"]


def test_no_marca_cifras_que_ya_estaban():
    assert find_invented_numbers("Bajé la latencia de 800 ms a 210 ms",
                                 ["Reduje la latencia de 800 ms a 210 ms"]) == []


def test_ignora_separadores_de_miles():
    """'12.000' y '12,000' son el mismo número: marcar eso sería un falso positivo."""
    assert find_invented_numbers("Procesa 12,000 transacciones",
                                 ["Procesa 12.000 transacciones"]) == []


def test_ignora_años():
    assert find_invented_numbers("Desde 2021 lideré el equipo", ["Lideré el equipo"]) == []


def test_merge_copia_los_hechos_verbatim():
    """El modelo nunca emite empresa ni fechas: se copian del parse."""
    patch = {"entries": [{"id": "exp0", "keep": True, "bullets": ["Optimicé el checkout"]}]}
    final, _ = merge_patch(PARSED, patch)
    assert final["experience"][0]["organization"] == "Acme"
    assert final["experience"][0]["dates"] == "2021-hoy"
    assert final["experience"][0]["bullets"] == ["Optimicé el checkout"]


def test_bullets_vacios_conservan_los_originales():
    patch = {"entries": [{"id": "exp0", "keep": True, "bullets": []}]}
    final, _ = merge_patch(PARSED, patch)
    assert final["experience"][0]["bullets"] == PARSED["experience"][0]["bullets"]


def test_descarta_skills_inventadas_y_dedupea_las_difusas():
    patch = {"entries": [], "skills": ["Postgres", "Kubernetes"]}
    final, warnings = merge_patch(PARSED, patch)
    assert "Kubernetes" not in final["skills"]
    assert "Postgres" in final["skills"] and "PostgreSQL" not in final["skills"]
    assert any(w["kind"] == "invented_skill" for w in warnings)


def test_avisa_cuando_omite_una_entrada():
    patch = {"entries": [{"id": "exp1", "keep": False}]}
    final, warnings = merge_patch(PARSED, patch)
    assert [e["organization"] for e in final["experience"]] == ["Acme"]
    assert any(w["kind"] == "dropped_entry" for w in warnings)


def test_el_orden_del_patch_manda():
    patch = {"entries": [{"id": "exp1", "keep": True}, {"id": "exp0", "keep": True}]}
    final, _ = merge_patch(PARSED, patch)
    assert [e["organization"] for e in final["experience"]] == ["Vieja SA", "Acme"]


def test_match_report_cuenta_los_avisos():
    patch = {"entries": [{"id": "exp0", "bullets": ["Subí la conversión un 40%"]}],
             "matched_keywords": ["Python"], "missing_requirements": ["Go"]}
    _, warnings = merge_patch(PARSED, patch)
    report = build_match_report(patch, warnings)
    assert report["score"] == 50 and report["flagged_numbers"] == 1
