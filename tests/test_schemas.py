"""Los schemas descartan lo malo en vez de tumbar el job."""

from apps.llm.schemas import ParsedCv, TailorPatch


def test_coerce_tipos_raros_sin_romper():
    parsed = ParsedCv.model_validate({
        "contact": "Ana Gómez",                     # string donde va un objeto
        "experience": {"organization": "Acme", "bullets": "uno\ndos"},  # dict y string
        "skills": [{"name": "Python"}, "SQL", None],  # dicts envoltorio y nulls
        "education": None,
    })
    assert parsed.contact.full_name == ""           # el dato malo se descarta
    assert parsed.experience[0].bullets == ["uno", "dos"]
    assert parsed.skills == ["Python", "SQL"]
    assert parsed.education == []


def test_keep_acepta_el_string_false_de_los_modelos_flojos():
    patch = TailorPatch.model_validate({"entries": [{"id": "exp0", "keep": "false"}]})
    assert patch.entries[0].keep is False


def test_campos_faltantes_caen_a_default():
    """Si la respuesta se trunca, la cola perdida no puede romper el merge."""
    patch = TailorPatch.model_validate({"entries": [{"id": "exp0", "bullets": ["x"]}]})
    assert patch.headline == "" and patch.missing_requirements == []
