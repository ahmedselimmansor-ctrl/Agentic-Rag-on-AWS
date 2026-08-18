"""OCR fallback for PDFs with no embedded text layer.

A scanned PDF extracts as zero characters through pypdf. Rather than failing the
document, ingestion falls back to AWS Textract when it is available. Textract is
billed per page, so this only runs when normal extraction genuinely produced
nothing — never as the primary path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from app.config import settings

logger = logging.getLogger(__name__)

# Textract's synchronous API accepts a single page of up to 5 MB.
SYNC_MAX_BYTES = 5 * 1024 * 1024


class OCRUnavailable(RuntimeError):
    """OCR is not configured or not reachable — the caller should degrade."""


@dataclass(slots=True)
class OCRPage:
    page: int
    text: str


def is_available() -> bool:
    """OCR needs S3-backed storage: Textract's async API reads from a bucket."""
    return settings.upload_backend == "s3" and bool(settings.s3_bucket)


def extract_pdf(storage_uri: str) -> list[OCRPage]:
    """Return per-page text. Raises OCRUnavailable when OCR cannot run."""
    if not is_available():
        raise OCRUnavailable(
            "OCR requires S3 storage (UPLOAD_BACKEND=s3). Scanned PDFs cannot be "
            "ingested with local storage."
        )

    parsed = urlparse(storage_uri)
    if parsed.scheme != "s3":
        raise OCRUnavailable(f"Textract needs an s3:// URI, got {parsed.scheme}://")

    bucket, key = parsed.netloc, parsed.path.lstrip("/")

    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - boto3 is a hard dependency
        raise OCRUnavailable("boto3 is not installed") from exc

    client = boto3.client("textract", region_name=settings.aws_region)

    try:
        start = client.start_document_text_detection(
            DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}}
        )
        job_id = start["JobId"]
    except Exception as exc:  # noqa: BLE001
        raise OCRUnavailable(f"Textract could not start: {exc}") from exc

    blocks = _await_job(client, job_id)
    return _blocks_to_pages(blocks)


def _await_job(client, job_id: str, *, timeout_seconds: int = 300) -> list[dict]:  # noqa: ANN001
    """Poll until the job finishes, following pagination."""
    import time

    deadline = time.monotonic() + timeout_seconds
    blocks: list[dict] = []
    next_token: str | None = None

    while True:
        if time.monotonic() > deadline:
            raise OCRUnavailable(f"Textract job {job_id} timed out after {timeout_seconds}s")

        kwargs = {"JobId": job_id}
        if next_token:
            kwargs["NextToken"] = next_token

        response = client.get_document_text_detection(**kwargs)
        job_status = response["JobStatus"]

        if job_status == "IN_PROGRESS":
            time.sleep(3)
            continue
        if job_status == "FAILED":
            raise OCRUnavailable(f"Textract failed: {response.get('StatusMessage', 'unknown')}")

        blocks.extend(response.get("Blocks", []))
        next_token = response.get("NextToken")
        if not next_token:
            return blocks


def _blocks_to_pages(blocks: list[dict]) -> list[OCRPage]:
    by_page: dict[int, list[str]] = {}
    for block in blocks:
        # LINE preserves reading order; WORD would need reassembly.
        if block.get("BlockType") != "LINE":
            continue
        page = int(block.get("Page", 1))
        text = (block.get("Text") or "").strip()
        if text:
            by_page.setdefault(page, []).append(text)

    return [OCRPage(page=p, text="\n".join(lines)) for p, lines in sorted(by_page.items())]
