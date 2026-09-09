#!/usr/bin/env bash
# Redespliegue a la instancia de EC2. Uso: ./deploy/redeploy.sh
#
# No toca el .env del servidor ni la base: solo sincroniza el codigo, reinstala
# dependencias si cambiaron, migra y reinicia los servicios.
set -euo pipefail

HOST="${CV_MODISTA_HOST:-ubuntu@44.213.3.220}"
APP=/opt/cv-modista/app

echo "==> Sincronizando codigo a $HOST"
rsync -az --delete \
  --exclude '.venv' --exclude '.git' --exclude '__pycache__' --exclude '*.sqlite3*' \
  --exclude 'media' --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '.env' \
  --exclude 'staticfiles' \
  ./ "$HOST:/tmp/cv-modista-app/"

ssh "$HOST" 'sudo bash -s' <<'REMOTE'
set -euo pipefail
APP=/opt/cv-modista/app
# --delete pero preservando .env, la base y el venv, que no viajan en el rsync.
rsync -a --delete \
  --exclude '.env' --exclude 'db.sqlite3*' --exclude '.venv' \
  /tmp/cv-modista-app/ "$APP/"
chown -R cvmodista:cvmodista "$APP"
rm -rf /tmp/cv-modista-app

sudo -u cvmodista bash -c "
  set -euo pipefail
  cd $APP && set -a && source .env && set +a
  .venv/bin/pip install --quiet --upgrade django 'celery[redis]' redis httpx pydantic \
    python-dotenv jinja2 json-repair pypdf python-docx rapidfuzz boto3 django-storages \
    whitenoise gunicorn pypdfium2 pillow
  .venv/bin/python manage.py migrate --noinput
  .venv/bin/python manage.py collectstatic --noinput
"
chmod -R a+rX /var/www/cv-modista/static
chmod 640 "$APP/db.sqlite3" 2>/dev/null || true

systemctl restart cv-modista-web cv-modista-worker cv-modista-beat
sleep 4
for unit in cv-modista-web cv-modista-worker cv-modista-beat; do
  printf "%-22s %s\n" "$unit" "$(systemctl is-active $unit)"
done
REMOTE

echo "==> Verificando"
curl -fsS -o /dev/null -w "  https -> %{http_code}\n" https://cv-assistant.tereredev.com/cuenta/entrar/
echo "==> Listo"
