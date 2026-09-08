"""Subida directa del navegador a S3 con URL prefirmada.

Se usa POST prefirmado y no PUT a propósito: el POST admite condiciones, y eso permite
imponer un `content-length-range` que S3 hace cumplir del lado del servidor. Con un PUT
prefirmado el tamaño no se puede limitar y cualquiera con la URL sube lo que quiera.

La key siempre lleva el id del usuario adelante y se valida contra el usuario de la
sesión, así nadie puede reclamar un objeto ajeno mandando la key de otro.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from django.conf import settings

log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/plain",
}


class UploadError(ValueError):
    pass


def enabled() -> bool:
    return bool(settings.USE_S3)


def _client():
    import boto3

    return boto3.client("s3", region_name=settings.AWS_S3_REGION_NAME)


def build_key(user_id: int, filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise UploadError(
            f"Extensión no permitida: {suffix or 'sin extensión'}. Usá PDF, DOCX o TXT."
        )
    return f"uploads/{user_id}/{uuid.uuid4().hex}{suffix}"


def presign(user_id: int, filename: str) -> dict:
    """Devuelve {url, fields, key} para que el navegador postee el archivo a S3."""
    if not enabled():
        raise UploadError("S3 no está configurado en este entorno.")

    key = build_key(user_id, filename)
    content_type = CONTENT_TYPES[Path(filename).suffix.lower()]
    presigned = _client().generate_presigned_post(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, settings.PRESIGNED_MAX_BYTES],
        ],
        ExpiresIn=settings.PRESIGNED_UPLOAD_EXPIRE,
    )
    return {"url": presigned["url"], "fields": presigned["fields"], "key": key}


def fetch(user_id: int, key: str) -> tuple[str, bytes]:
    """Baja el objeto subido. Devuelve (nombre, bytes)."""
    if not enabled():
        raise UploadError("S3 no está configurado en este entorno.")
    # El prefijo con el id del usuario es la autorización: sin esto, mandar la key de
    # otro alcanzaría para leer su CV.
    if not key.startswith(f"uploads/{user_id}/"):
        raise UploadError("Esa subida no te pertenece.")

    try:
        response = _client().get_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)
    except Exception as exc:  # noqa: BLE001 - boto3 tira ClientError y variantes
        raise UploadError(f"No se pudo leer el archivo subido: {exc}") from exc

    if response["ContentLength"] > settings.PRESIGNED_MAX_BYTES:
        raise UploadError("El archivo supera el tamaño permitido.")
    return Path(key).name, response["Body"].read()
