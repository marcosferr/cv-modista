# Infraestructura

Todo en una sola instancia EC2. No hay ALB ni RDS a propósito: para una app con este
volumen serían unos USD 35/mes extra sin ganar nada, y nginx con Let's Encrypt hace el
mismo trabajo que un ALB con ACM.

## Recursos

| Recurso | Identificador |
|---|---|
| Instancia | `i-0302070467881bdcc` — t4g.medium (2 vCPU / 4 GB, Graviton), Ubuntu 24.04 ARM64 |
| IP elástica | `44.213.3.220` |
| Dominio | `cv-assistant.tereredev.com` (DNS fuera de esta cuenta) |
| Bucket | `cv-modista-media-784956360606` — privado, cifrado, versionado |
| Security group | `sg-0b5343788a4193b5b` — 80/443 público, 22 solo desde la IP de marcos |
| Rol de instancia | `cv-modista-ec2-role` — solo este bucket, más SSM |
| Región | us-east-1 |

## Servicios en la instancia

| Unidad systemd | Qué hace |
|---|---|
| `cv-modista-web` | gunicorn en 127.0.0.1:8001, 3 workers |
| `cv-modista-worker` | Celery, `--concurrency=1` |
| `cv-modista-beat` | Resincroniza el pool de modelos `:free` cada 6 h |
| `nginx` | TLS y proxy inverso |
| `redis-server` | Broker de Celery y store de rotación/cupo |

El worker va con concurrencia 1 a propósito: el cuello de botella es el cupo de
OpenRouter, no la CPU, y compilar LaTeX en paralelo solo compite por memoria.

## Costo aproximado

t4g.medium ~USD 24/mes + 20 GB gp3 ~USD 1,60 + IP elástica asociada (gratis) + S3 por
uso. Los CVs crudos se borran solos a los 30 días por una regla de lifecycle.

## Operación

```bash
./deploy/redeploy.sh                  # sincroniza codigo, migra y reinicia
ssh ubuntu@44.213.3.220
sudo journalctl -u cv-modista-worker -f    # ver el pipeline en vivo
sudo systemctl restart cv-modista-web
```

Si tu IP cambia y el SSH deja de entrar, hay dos salidas: actualizar la regla del
security group, o entrar por SSM (`aws ssm start-session --target i-0302070467881bdcc
--profile marcos`), que el rol ya tiene habilitado.

El certificado lo renueva el timer de certbot que instala el paquete. Para verificarlo:
`sudo certbot renew --dry-run`.

## Acceso a la base

SQLite en `/opt/cv-modista/app/db.sqlite3`, en modo WAL para que web y worker escriban
sin pisarse. Backup: `scp ubuntu@44.213.3.220:/opt/cv-modista/app/db.sqlite3 .` (pará
los servicios antes, o usá `sqlite3 db.sqlite3 ".backup respaldo.db"`).
