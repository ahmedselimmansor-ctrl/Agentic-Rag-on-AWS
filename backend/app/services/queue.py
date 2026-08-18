"""Ingestion work queue.

Two modes, chosen by `INGESTION_MODE`:

- `inline` — a FastAPI background task in the API process. Zero infrastructure,
  correct for local development and small deployments.
- `sqs`    — hands the job to a dedicated worker service. Embedding a 200-page
  PDF is minutes of CPU and provider latency; running that in the API process
  means it competes with streaming turns for the event loop, and an API deploy
  mid-ingest loses the job silently.

Delivery is at-least-once, which is safe here because `ingest_document` replaces
a document's chunks rather than appending: reprocessing converges on the same
result instead of duplicating passages.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

INGEST_DOCUMENT = "ingest_document"


class QueueUnavailable(RuntimeError):
    pass


def _sqs():  # noqa: ANN202 - boto3 client is untyped
    import boto3

    return boto3.client("sqs", region_name=settings.aws_region)


def is_sqs_enabled() -> bool:
    return settings.ingestion_mode == "sqs" and bool(settings.ingestion_queue_url)


def publish_ingestion(document_id: uuid.UUID) -> bool:
    """Enqueue an ingestion job. Returns False when SQS is not in use, so the
    caller can fall back to running it inline."""
    if not is_sqs_enabled():
        return False

    body = {"task": INGEST_DOCUMENT, "document_id": str(document_id)}
    try:
        _sqs().send_message(
            QueueUrl=settings.ingestion_queue_url,
            MessageBody=json.dumps(body),
            # Groups by document so retries of the same doc stay ordered on a
            # FIFO queue; ignored by standard queues.
            MessageAttributes={
                "task": {"StringValue": INGEST_DOCUMENT, "DataType": "String"}
            },
        )
    except Exception as exc:  # noqa: BLE001
        # Losing the job outright is worse than a slow API request, so the
        # caller degrades to inline processing.
        logger.error("failed to enqueue ingestion for %s: %s", document_id, exc)
        return False

    logger.info("enqueued ingestion for %s", document_id)
    return True


def receive_messages(max_messages: int = 1, wait_seconds: int = 20) -> list[dict[str, Any]]:
    """Long-poll for jobs. Long polling avoids burning API calls on an idle queue."""
    if not is_sqs_enabled():
        raise QueueUnavailable("INGESTION_MODE is not 'sqs' or INGESTION_QUEUE_URL is unset")

    response = _sqs().receive_message(
        QueueUrl=settings.ingestion_queue_url,
        MaxNumberOfMessages=max(1, min(max_messages, 10)),
        WaitTimeSeconds=max(0, min(wait_seconds, 20)),
        VisibilityTimeout=settings.ingestion_visibility_timeout,
        MessageAttributeNames=["All"],
    )
    return response.get("Messages", [])


def delete_message(receipt_handle: str) -> None:
    _sqs().delete_message(
        QueueUrl=settings.ingestion_queue_url, ReceiptHandle=receipt_handle
    )


def extend_visibility(receipt_handle: str, seconds: int) -> None:
    """Push out the visibility deadline for a job still in progress, so a slow
    document is not redelivered to a second worker while the first is working."""
    _sqs().change_message_visibility(
        QueueUrl=settings.ingestion_queue_url,
        ReceiptHandle=receipt_handle,
        VisibilityTimeout=max(0, min(seconds, 43_200)),
    )


def parse_message(message: dict[str, Any]) -> tuple[str, uuid.UUID] | None:
    """Returns (task, document_id), or None when the body is unusable — a
    malformed message must be dropped, not retried until it hits the DLQ."""
    try:
        body = json.loads(message["Body"])
        return body["task"], uuid.UUID(body["document_id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.error("unparseable queue message %s: %s", message.get("MessageId"), exc)
        return None
