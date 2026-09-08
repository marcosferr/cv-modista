"""Precalienta la cache de paquetes de Tectonic.

La primera compilacion baja el bundle y tarda ~30s, mas que el timeout normal. En un
servidor nuevo conviene correr esto en el deploy y no dejar que lo pague el primer job.
"""

import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.resume.compile import LatexError, TectonicCompiler
from apps.resume.render import render_tex

MUESTRA = {
    "contact": {"full_name": "Warm Up", "email": "a@b.com", "location": "Asunción"},
    "summary": "", "extras": [], "skills": ["Python"],
    "experience": [{"organization": "Acme", "title": "Dev", "location": "Asunción",
                    "dates": "2020 - 2024", "bullets": ["Un bullet con acentos: ñ, á, é"]}],
    "education": [{"institution": "UNA", "degree": "Ingeniería", "location": "Asunción",
                   "dates": "2014 - 2019", "details": []}],
}


class Command(BaseCommand):
    help = "Compila un CV de muestra para dejar la cache de Tectonic lista."

    def handle(self, *args, **options):
        compiler = TectonicCompiler(timeout=600)
        if not compiler.available():
            raise CommandError("Tectonic no está instalado.")
        workdir = Path(tempfile.mkdtemp(prefix="warm-latex-"))
        self.stdout.write("Compilando muestra (la primera vez baja el bundle, ~30s)…")
        try:
            pdf = compiler.compile(render_tex(MUESTRA), workdir)
        except LatexError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Cache lista. PDF de prueba: {pdf.stat().st_size} bytes"))
