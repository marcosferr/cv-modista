# cv-modista

Pegás el puesto al que aplicás, el aviso de LinkedIn y tu CV actual. Sale un CV en
formato Harvard adaptado a esa oferta, descargable **en PDF y en LaTeX**, más un pack de
LinkedIn. Todo procesado en cola.

**En producción:** https://cv-assistant.tereredev.com

## Lo que define el diseño

Corre entero sobre los modelos `:free` de OpenRouter. Ese tier tiene un cupo diario **por
cuenta** (rotar de modelo no lo multiplica: OpenRouter lo gobierna globalmente), 20
req/min, y modelos que truncan la salida, desaparecen de la lista y a veces devuelven
basura. De ahí salen las tres decisiones centrales:

**1. La segunda llamada emite un patch, no un CV.** El modo de fallo número uno no es el
JSON malformado, es el truncamiento: reemitir un CV entero como JSON pega contra
`finish_reason: "length"`. Así que el parse asigna ids estables a cada entrada y bullet,
y el tailor devuelve solo `{entry_id, keep, bullets[]}`. Empresas, cargos, fechas e
instituciones se copian verbatim en Python. Los tokens de salida bajan ~4x, y **inventar
un empleador es estructuralmente imposible**: el modelo nunca emite uno.

**2. El parse se cachea por hash del CV.** No depende de la oferta, así que postularse a
diez puestos con el mismo CV cuesta una sola llamada de parse. El análisis del aviso va
plegado dentro de la llamada de tailoring. Resultado: ~1 llamada por CV.

**3. El LLM nunca escribe LaTeX.** Emite JSON y una plantilla Jinja2 con escapado
estricto lo convierte en `.tex`. Eso mata de una la inyección de LaTeX desde texto del
usuario y el clásico "el modelo generó LaTeX que no compila".

## Guardas contra la debilidad de los modelos free

| Guarda | Qué resuelve |
|---|---|
| Escalera de recuperación de JSON | Fences markdown, prosa alrededor, bloques de razonamiento y **respuestas truncadas** (json-repair cierra lo que quedó abierto). Costo cero de cupo. |
| Anillo de rotación con cooldown | Un 429 del proveedor manda ese modelo a 90 s de banquillo; un JSON irrecuperable, a 30 min; un 404 sin endpoints, a 24 h. |
| Clasificación de 429 | Distingue el throttling del proveedor (rotar) del cupo diario agotado (no reintentar, esperar a medianoche UTC) leyendo `metadata.limit_source`. |
| Detección de cifras inventadas | Cada token numérico de un bullet reescrito tiene que existir en el original. "Reduje la latencia" convertido en "un 40%" queda marcado. |
| Detector de contaminación | Descarta al modelo que copia el ejemplo del prompt en vez de generar. Pasa el schema sin problemas, así que ninguna validación de forma lo agarra. |
| Heurística de tamaño | Un modelo de 2.6B recorta el CV sin criterio y copia el ejemplo. Queda como último recurso, nunca primero. |

## El `.tex` es descargable, así que tiene que valer en Overleaf

Tectonic usa XeTeX y Overleaf usa pdfLaTeX por defecto, así que la regla vinculante es
que el mismo archivo compile sin cambios con **pdflatex, xelatex y lualatex**. Eso
prohíbe `fontspec`. `fontenc` va fuera del guard de `iftex` (con TU activa, `tgtermes`
no toma efecto y `\bfseries` no encuentra la negrita: el CV sale entero en Latin Modern
regular), y `cmap` va adentro, para que un ATS pueda extraer los acentos en vez de leer
"Formaci n Acad mica".

Sin foto, una columna, sin iconos. Después de compilar se verifica que el nombre, cada
empresa y cada rango de fechas se puedan extraer del PDF: es literalmente lo que hace un
ATS. Si sale de más de una página, se recortan bullets del rol más viejo y se recompila,
sin gastar LLM.

## Correr en local

```bash
make install          # deps de Python, tectonic y redis
cp .env.example .env  # poné tu OPENROUTER_API_KEY
make migrate
make warm             # precalienta la cache de Tectonic (la primera vez baja ~30 s)
make dev              # redis + worker + server
```

```bash
make test             # 68 tests contra fixtures, sin gastar una llamada
make probe            # prueba un modelo real. Gasta 1 llamada.
make probe MODEL=nvidia/nemotron-3-super-120b-a12b:free
.venv/bin/python manage.py probe_model --pool   # estado del pool y del cupo
```

Con 50 llamadas diarias no se puede iterar un prompt clickeando la UI, así que
`FAKE_LLM=1` corre todo contra `fixtures/llm/`. El renderer, el compilador y el gate
anti-invento se prueban enteros sin tocar la API.

## Estructura

```
config/            settings, celery, urls
apps/llm/          cliente de OpenRouter, rotación, cupo, prompts, schemas, escalera de JSON
apps/resume/       merge del patch, plantilla LaTeX, compilación, verificación ATS
apps/jobs/         modelos, formularios, vistas, tasks de Celery, extracción de texto
deploy/            nginx, systemd, script de redespliegue (ver deploy/README.md)
fixtures/llm/      respuestas de ejemplo para FAKE_LLM
```

## Notas

- **Extracción del CV**: pypdf primero (Python puro, cubre los PDFs digitales). Si saca
  menos de 500 caracteres cae a OCR con `rapidocr-onnxruntime`, que son ~40 MB por pip
  sin binarios de sistema. Se descartó tesseract (binario de ~120 MB) y easyocr (arrastra
  torch).
- **Subida a S3**: POST prefirmado, no PUT. El POST admite condiciones, así que S3 hace
  cumplir el límite de 10 MB del lado del servidor. Con un PUT prefirmado el tamaño no se
  puede limitar.
- **Editar y recompilar**: la vista de revisión deja editar el JSON final y recompilar
  **sin ninguna llamada al LLM**. Con modelos flojos es la función más útil: el modelo
  llega al 80% y el resto lo corregís al instante.
