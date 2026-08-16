"""Blob storage — local filesystem for dev, S3 in AWS.

Uploaded bytes never live in Postgres; only the `storage_uri` does. Images are
re-read at embed time and, for vision-capable turns, presigned for the model.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StoredObject:
    uri: str
    size_bytes: int
    sha256: str
    mime_type: str


def _s3():  # noqa: ANN202 - boto3 client is untyped
    import boto3

    return boto3.client("s3", region_name=settings.aws_region)


def store_bytes(data: bytes, filename: str, *, content_type: str | None = None) -> StoredObject:
    digest = hashlib.sha256(data).hexdigest()
    mime = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    # Content-addressed key: re-uploading the same file is a no-op.
    safe_name = Path(filename).name.replace("/", "_")
    key = f"{settings.s3_prefix}{digest[:2]}/{digest}/{safe_name}"

    if settings.upload_backend == "s3":
        if not settings.s3_bucket:
            raise RuntimeError("S3_BUCKET is not set but UPLOAD_BACKEND=s3")
        _s3().put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=data,
            ContentType=mime,
            ServerSideEncryption="AES256",
        )
        uri = f"s3://{settings.s3_bucket}/{key}"
    else:
        dest = Path(settings.upload_dir) / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        uri = f"file://{dest}"

    return StoredObject(uri=uri, size_bytes=len(data), sha256=digest, mime_type=mime)


def download_to_temp(uri: str) -> str:
    """Materialise an object on local disk. Caller is responsible for cleanup."""
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return parsed.path
    if parsed.scheme == "s3":
        bucket, key = parsed.netloc, parsed.path.lstrip("/")
        suffix = Path(key).suffix
        fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="rag-")
        os.close(fd)
        _s3().download_file(bucket, key, tmp)
        return tmp
    raise ValueError(f"Unsupported storage URI scheme: {uri}")


def presigned_url(uri: str, expires: int | None = None) -> str:
    """A URL the embedding/vision model can fetch. Local files return a data URI."""
    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        return _s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": parsed.netloc, "Key": parsed.path.lstrip("/")},
            ExpiresIn=expires or settings.presign_expiry_seconds,
        )
    if parsed.scheme == "file":
        import base64

        path = parsed.path
        mime = mimetypes.guess_type(path)[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(Path(path).read_bytes()).decode()}"
    return uri


def delete(uri: str) -> None:
    parsed = urlparse(uri)
    try:
        if parsed.scheme == "s3":
            _s3().delete_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))
        elif parsed.scheme == "file":
            Path(parsed.path).unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - orphaned blobs are not worth failing a request
        logger.warning("failed to delete %s: %s", uri, exc)


def cleanup_temp(path: str, original_uri: str) -> None:
    """Remove a temp file only if we created it (i.e. it came from S3)."""
    if urlparse(original_uri).scheme != "file":
        Path(path).unlink(missing_ok=True)


def new_upload_id() -> str:
    return uuid.uuid4().hex
