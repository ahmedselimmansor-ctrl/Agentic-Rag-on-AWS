"""Ingestion worker.

Runs as its own ECS service (`python -m app.worker`) so document processing
never competes with streaming turns for the API's event loop, and an API deploy
cannot kill a job mid-embed.

Shutdown is graceful: SIGTERM stops the poll loop but lets the in-flight job
finish. ECS sends SIGTERM then waits `stopTimeout` before SIGKILL, so a job that
finishes inside that window is not redelivered.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time

from app.config import settings
from app.core.logging import configure_logging, log_extra
from app.db.session import dispose_engine
from app.services import queue
from app.services.http import close_client
from app.services.ingestion import ingest_document

logger = logging.getLogger(__name__)

# Re-extend the message's visibility while a long document is still processing.
HEARTBEAT_INTERVAL = 60
IDLE_BACKOFF_SECONDS = 1.0


class Worker:
    def __init__(self) -> None:
        self._running = True
        self._processed = 0
        self._failed = 0

    def request_stop(self, signum: int, _frame: object) -> None:
        logger.info("received signal %s, finishing current job then exiting", signum)
        self._running = False

    async def run(self) -> None:
        configure_logging(settings.log_level)

        if not queue.is_sqs_enabled():
            logger.error(
                "worker requires INGESTION_MODE=sqs and INGESTION_QUEUE_URL; exiting"
            )
            return

        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

        logger.info(
            "ingestion worker started",
            extra=log_extra(queue_url=settings.ingestion_queue_url),
        )

        try:
            while self._running:
                await self._poll_once()
        finally:
            logger.info(
                "worker stopped",
                extra=log_extra(processed=self._processed, failed=self._failed),
            )
            await close_client()
            await dispose_engine()

    async def _poll_once(self) -> None:
        try:
            # receive_message blocks on the network; keep it off the event loop.
            messages = await asyncio.to_thread(
                queue.receive_messages, settings.ingestion_batch_size, 20
            )
        except Exception as exc:  # noqa: BLE001 - a transient SQS error must not kill the worker
            logger.error("failed to poll queue: %s", exc)
            await asyncio.sleep(5)
            return

        if not messages:
            await asyncio.sleep(IDLE_BACKOFF_SECONDS)
            return

        for message in messages:
            if not self._running:
                # Leave unprocessed messages on the queue; visibility expiry
                # redelivers them to whichever worker is still alive.
                break
            await self._handle(message)

    async def _handle(self, message: dict) -> None:
        receipt = message["ReceiptHandle"]
        parsed = queue.parse_message(message)

        if parsed is None:
            # Unparseable: delete rather than let it cycle to the DLQ. It will
            # never succeed and redelivering it just burns worker time.
            await asyncio.to_thread(queue.delete_message, receipt)
            return

        task, document_id = parsed
        if task != queue.INGEST_DOCUMENT:
            logger.warning("unknown task %r, dropping", task)
            await asyncio.to_thread(queue.delete_message, receipt)
            return

        started = time.perf_counter()
        heartbeat = asyncio.create_task(self._heartbeat(receipt))

        try:
            result = await ingest_document(document_id)
            duration_ms = int((time.perf_counter() - started) * 1000)

            # ingest_document reports failure in its return value rather than
            # raising, and a parse failure will fail identically next time — so
            # the message is removed either way. The document row carries the
            # error for the UI.
            await asyncio.to_thread(queue.delete_message, receipt)

            if result.status.value == "failed":
                self._failed += 1
                logger.warning(
                    "ingestion failed",
                    extra=log_extra(
                        document_id=str(document_id), error=result.error, duration_ms=duration_ms
                    ),
                )
            else:
                self._processed += 1
                logger.info(
                    "ingestion complete",
                    extra=log_extra(
                        document_id=str(document_id),
                        chunks=result.chunk_count,
                        duration_ms=duration_ms,
                    ),
                )
        except Exception:  # noqa: BLE001
            # An unexpected error (OOM, dropped DB connection) may well succeed
            # on retry, so the message is left for redelivery.
            self._failed += 1
            logger.exception("unhandled error ingesting %s; leaving job for retry", document_id)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, receipt: str) -> None:
        """Keep extending the visibility timeout until the job finishes."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await asyncio.to_thread(
                    queue.extend_visibility, receipt, settings.ingestion_visibility_timeout
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not extend message visibility: %s", exc)
                return


def main() -> None:
    asyncio.run(Worker().run())


if __name__ == "__main__":
    main()
