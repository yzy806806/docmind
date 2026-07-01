"""PostgreSQL-backed job queue with SKIP LOCKED concurrency pattern.

This module provides the ``JobQueue`` class — a dedicated job queue built on
PostgreSQL's ``FOR UPDATE SKIP LOCKED`` feature. Multiple workers can poll
concurrently without conflicts; each claims a unique job row.

Design:
- ``enqueue()`` — insert a pending job row.
- ``dequeue()`` — claim the oldest pending job atomically.
- ``complete()`` / ``fail()`` — transition a job to terminal states.
- ``get_status()`` — query a job's current state.
- ``run_worker()`` — long-running async worker loop.

The queue is durable: jobs survive restarts because state lives in PostgreSQL.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .db import Database
from .models import JobRecord, JobState

logger = logging.getLogger(__name__)


class JobQueue:
    """Async job queue backed by PostgreSQL SKIP LOCKED.

    Usage::

        db = Database(dsn="postgresql://...")
        await db.connect()
        queue = JobQueue(db)

        # Producer
        job = await queue.enqueue("/data/report.pdf", title="Q4 Report")

        # Consumer
        claimed = await queue.dequeue()
        if claimed:
            try:
                do_work(claimed)
                await queue.complete(claimed.id, doc_id=42)
            except Exception as exc:
                await queue.fail(claimed.id, str(exc))
    """

    def __init__(self, database: Database):
        """Wrap an existing Database instance.

        Args:
            database: A connected ``Database`` instance (must have called .connect()).
        """
        self._db = database

    # ── Producer API ────────────────────────────────────────────

    async def enqueue(
        self,
        document_path: str,
        *,
        document_title: Optional[str] = None,
        source_name: str = "api",
    ) -> JobRecord:
        """Insert a new job into the queue.

        Args:
            document_path: Path or URL of the document to process.
            document_title: Optional display title.
            source_name: Provenance label (e.g. "api", "webdav").

        Returns:
            The newly created ``JobRecord`` with state=pending.
        """
        logger.debug("Enqueuing job for %s", document_path)
        return await self._db.enqueue_job(
            document_path,
            document_title=document_title,
            source_name=source_name,
        )

    # ── Consumer API ────────────────────────────────────────────

    async def dequeue(self) -> Optional[JobRecord]:
        """Claim the oldest pending job atomically.

        Uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers never
        claim the same job.

        Returns:
            A ``JobRecord`` in state=processing, or ``None`` if the queue is empty.
        """
        job = await self._db.dequeue_job()
        if job:
            logger.debug("Dequeued job %s for %s", job.id, job.document_path)
        return job

    async def complete(self, job_id: str, document_id: int) -> None:
        """Mark a job as completed, linking it to the created document.

        Args:
            job_id: The job UUID.
            document_id: The internal document ID created during processing.
        """
        logger.debug("Completing job %s → document %d", job_id, document_id)
        await self._db.complete_job(job_id, document_id)

    async def fail(self, job_id: str, error: str) -> None:
        """Mark a job as failed with an error message.

        Args:
            job_id: The job UUID.
            error: Human-readable error description.
        """
        logger.debug("Failing job %s: %s", job_id, error)
        await self._db.fail_job(job_id, error)

    async def get_status(self, job_id: str) -> Optional[JobRecord]:
        """Query the current state of a job.

        Args:
            job_id: The job UUID.

        Returns:
            The ``JobRecord`` or ``None`` if not found.
        """
        return await self._db.get_job(job_id)

    # ── Worker loop ─────────────────────────────────────────────

    async def run_worker(
        self,
        *,
        process_fn,  # async callable: (JobRecord) -> int  (returns document_id)
        poll_interval: float = 2.0,
        max_retries: int = 3,
    ) -> None:
        """Run a long-lived worker loop that polls the queue forever.

        Each iteration:
        1. Attempt to dequeue a job.
        2. If a job is claimed, call ``process_fn(job)``.
        3. On success, call ``complete(job_id, document_id)``.
        4. On failure, retry up to ``max_retries`` times, then ``fail()``.
        5. Sleep ``poll_interval`` seconds before polling again.

        Args:
            process_fn: Async callable that takes a ``JobRecord`` and returns
                        the created ``document_id`` (int). Raise an exception
                        to signal failure.
            poll_interval: Seconds to sleep between poll iterations.
            max_retries: Maximum processing attempts before marking failed.

        Note:
            This method never returns unless cancelled. Wrap with
            ``asyncio.create_task()`` and cancel when shutting down.
        """
        logger.info(
            "Worker started (poll=%.1fs, max_retries=%d)",
            poll_interval, max_retries,
        )

        while True:
            try:
                job = await self.dequeue()
                if job is None:
                    await asyncio.sleep(poll_interval)
                    continue

                # Process with retries
                last_error: Optional[str] = None
                for attempt in range(1, max_retries + 1):
                    try:
                        logger.debug(
                            "Processing job %s (attempt %d/%d)",
                            job.id, attempt, max_retries,
                        )
                        document_id = await process_fn(job)
                        await self.complete(job.id, document_id)
                        logger.info(
                            "Job %s completed → document %d", job.id, document_id
                        )
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        logger.warning(
                            "Job %s attempt %d/%d failed: %s",
                            job.id, attempt, max_retries, exc,
                        )
                        if attempt < max_retries:
                            await asyncio.sleep(1.0 * attempt)  # linear backoff
                else:
                    # All retries exhausted
                    await self.fail(job.id, last_error or "Unknown error")
                    logger.error("Job %s permanently failed: %s", job.id, last_error)

            except asyncio.CancelledError:
                logger.info("Worker cancelled, shutting down")
                raise
            except Exception as exc:
                logger.exception("Worker loop error (will retry): %s", exc)
                await asyncio.sleep(poll_interval)

    async def run_workers(
        self,
        count: int,
        *,
        process_fn,  # async callable: (JobRecord) -> int
        poll_interval: float = 2.0,
        max_retries: int = 3,
    ) -> list[asyncio.Task]:
        """Start multiple worker coroutines in parallel.

        Args:
            count: Number of concurrent workers.
            process_fn: Async processing callable (see ``run_worker``).
            poll_interval: Poll interval per worker.
            max_retries: Max retries per job.

        Returns:
            List of ``asyncio.Task`` objects. Await or cancel as needed.
        """
        logger.info("Starting %d workers", count)
        tasks: list[asyncio.Task] = []
        for i in range(count):
            task = asyncio.create_task(
                self.run_worker(
                    process_fn=process_fn,
                    poll_interval=poll_interval,
                    max_retries=max_retries,
                ),
                name=f"docmind-worker-{i}",
            )
            tasks.append(task)
        return tasks
