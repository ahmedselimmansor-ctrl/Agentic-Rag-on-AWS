"""Queue message handling — the parts that decide whether a job is retried."""

from __future__ import annotations

import json
import uuid

from app.config import settings
from app.services import queue


def message(body: object) -> dict:
    return {"MessageId": "m-1", "ReceiptHandle": "r-1", "Body": json.dumps(body)}


def test_valid_message_parses():
    doc_id = uuid.uuid4()
    parsed = queue.parse_message(message({"task": "ingest_document", "document_id": str(doc_id)}))

    assert parsed == ("ingest_document", doc_id)


def test_malformed_bodies_return_none_rather_than_raising():
    """A message that can never be parsed must be droppable, not retried until
    it reaches the DLQ."""
    cases = [
        {"MessageId": "m", "ReceiptHandle": "r", "Body": "not json at all"},
        message({"task": "ingest_document"}),                       # no document_id
        message({"document_id": str(uuid.uuid4())}),                # no task
        message({"task": "ingest_document", "document_id": "nope"}),  # not a uuid
        message([]),                                                # wrong shape
    ]

    for case in cases:
        assert queue.parse_message(case) is None, case


def test_sqs_disabled_without_configuration(monkeypatch):
    monkeypatch.setattr(settings, "ingestion_mode", "inline")
    monkeypatch.setattr(settings, "ingestion_queue_url", "https://sqs.example/q")
    assert queue.is_sqs_enabled() is False

    monkeypatch.setattr(settings, "ingestion_mode", "sqs")
    monkeypatch.setattr(settings, "ingestion_queue_url", "")
    assert queue.is_sqs_enabled() is False

    monkeypatch.setattr(settings, "ingestion_queue_url", "https://sqs.example/q")
    assert queue.is_sqs_enabled() is True


def test_publish_reports_false_when_disabled(monkeypatch):
    """False tells the caller to fall back to inline processing rather than
    silently dropping the document."""
    monkeypatch.setattr(settings, "ingestion_mode", "inline")
    assert queue.publish_ingestion(uuid.uuid4()) is False


def test_publish_reports_false_when_sqs_errors(monkeypatch):
    monkeypatch.setattr(settings, "ingestion_mode", "sqs")
    monkeypatch.setattr(settings, "ingestion_queue_url", "https://sqs.example/q")

    class Boom:
        def send_message(self, **_: object) -> None:
            raise RuntimeError("network is down")

    monkeypatch.setattr(queue, "_sqs", lambda: Boom())

    # Degrade to inline rather than losing the job.
    assert queue.publish_ingestion(uuid.uuid4()) is False


def test_publish_sends_the_document_id(monkeypatch):
    monkeypatch.setattr(settings, "ingestion_mode", "sqs")
    monkeypatch.setattr(settings, "ingestion_queue_url", "https://sqs.example/q")

    sent: list[dict] = []

    class Recorder:
        def send_message(self, **kwargs: object) -> None:
            sent.append(kwargs)

    monkeypatch.setattr(queue, "_sqs", lambda: Recorder())

    doc_id = uuid.uuid4()
    assert queue.publish_ingestion(doc_id) is True
    assert len(sent) == 1

    body = json.loads(sent[0]["MessageBody"])
    assert body == {"task": "ingest_document", "document_id": str(doc_id)}
