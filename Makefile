.PHONY: help install dev web worker test lint migrate superuser warm probe clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Instala dependencias de Python y de sistema
	uv sync --extra ocr
	@command -v tectonic >/dev/null || brew install tectonic
	@command -v redis-server >/dev/null || brew install redis

migrate:  ## Aplica migraciones
	.venv/bin/python manage.py migrate

superuser:  ## Crea un usuario administrador
	.venv/bin/python manage.py createsuperuser

dev:  ## Levanta redis, el worker y el server juntos
	@redis-cli ping >/dev/null 2>&1 || (echo "Levantando redis..."; brew services start redis)
	.venv/bin/honcho start

web:  ## Solo el servidor de Django
	.venv/bin/python manage.py runserver

worker:  ## Solo el worker de Celery (pool solo: prefork es inestable en macOS)
	.venv/bin/celery -A config worker --loglevel=info --pool=solo

test:  ## Corre la suite contra fixtures, sin gastar cupo de OpenRouter
	.venv/bin/python -m pytest -q

lint:  ## Ruff
	.venv/bin/ruff check apps/ config/ tests/
	.venv/bin/ruff format --check apps/ config/ tests/ 2>/dev/null || true

warm:  ## Precalienta la cache de paquetes de Tectonic (la primera compilacion baja ~30s)
	.venv/bin/python manage.py warm_latex

probe:  ## Prueba un modelo real de OpenRouter. Gasta 1 llamada. make probe MODEL=...
	.venv/bin/python manage.py probe_model $(if $(MODEL),--model $(MODEL),)

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} + ; rm -rf .pytest_cache .ruff_cache
