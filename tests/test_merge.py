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


def test_nunca_descarta_un_empleo():
    """Visto en producción: el modelo borró 1 de 2 empleos reales. Un hueco laboral sin
    explicar hace más daño que una entrada poco relevante, y en un formato cronológico
    inverso el hueco se ve."""
    patch = {"entries": [{"id": "exp1", "keep": False}]}
    final, warnings = merge_patch(PARSED, patch)
    assert [e["organization"] for e in final["experience"]] == ["Acme", "Vieja SA"]
    assert any(w["kind"] == "drops_ignored" for w in warnings)


def test_nunca_descarta_formacion():
    patch = {"entries": [{"id": "edu0", "keep": False}]}
    final, _ = merge_patch(PARSED, patch)
    assert len(final["education"]) == 1


def test_descarta_un_extra_irrelevante():
    """En extras sí es legítimo: un curso viejo que no aporta puede salir."""
    parsed = {**PARSED, "extras": [
        {"title": "Curso de Excel", "organization": "X", "dates": "2015", "details": []},
        {"title": "AWS Solutions Architect", "organization": "AWS", "dates": "2023", "details": []},
    ]}
    patch = {"entries": [{"id": "xtr0", "keep": False}, {"id": "xtr1", "keep": True}]}
    final, warnings = merge_patch(parsed, patch)
    assert [e["title"] for e in final["extras"]] == ["AWS Solutions Architect"]
    assert any(w["kind"] == "dropped_entry" for w in warnings)


def test_ignora_los_descartes_cuando_son_demasiados():
    """Caso real: pidió descartar 6 de 7 extras, incluidas tres certificaciones AWS,
    en una postulación a arquitecto de plataforma. Eso no es criterio, es pereza."""
    extras = [{"title": f"Item {i}", "organization": "X", "dates": "2023", "details": []}
              for i in range(7)]
    parsed = {**PARSED, "extras": extras}
    patch = {"entries": [{"id": f"xtr{i}", "keep": i == 0} for i in range(7)]}
    final, warnings = merge_patch(parsed, patch)
    assert len(final["extras"]) == 7, "gutteó la sección en vez de editarla"
    assert any(w["kind"] == "drops_ignored" for w in warnings)
    assert not any(w["kind"] == "dropped_entry" for w in warnings)


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
