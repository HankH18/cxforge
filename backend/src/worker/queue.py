"""The enqueue seam between webhook ingress (producer) and the arq worker
(consumer) — ADR-002.

Expressed as a FastAPI dependency for exactly the reason
`portal/deps.py::get_helpdesk_port` is: it gives the ingress test suite a
place to substitute a recording double via `app.dependency_overrides`
instead of monkeypatching a module global, while the *production* path
still constructs a real arq pool. Removing the `queue.enqueue(...)` call
from the handler makes the recorder see nothing — which is the whole point
of the A7.1 sabotage check.

A pool is created and closed per enqueue rather than held for the app's
lifetime. That is deliberate for this scope: `backend/src/main.py` is owned
by another Wave-1 track, so adding a lifespan handler here would take a file
Track A does not own, and at demo volume one short-lived Redis connection
per webhook costs nothing.
"""

from __future__ import annotations

from typing import Protocol

from arq.connections import create_pool

from worker.jobs import TicketJob
from worker.settings import QUEUE_NAME, RUN_TICKET_TASK, redis_settings


class JobQueue(Protocol):
    """What ingress needs from a queue, and nothing more."""

    async def enqueue(self, job: TicketJob) -> None: ...


class ArqJobQueue:
    """The real queue: publishes onto Redis list `cxforge:jobs` under the
    arq task name `run_ticket` (both frozen in DESIGN §1.1).

    No `_job_id` is passed on purpose. arq would use it to deduplicate, and
    a stable id derived from `(ticket_id, comment_id)` would silently drop
    the *retry* that ADR-003 explicitly preserves: on failure the worker
    releases the `tickets_seen` row so re-firing the Zendesk trigger
    reprocesses the comment. Queue-level dedup would swallow that re-fire.
    Idempotency lives in Postgres, where it is durable.
    """

    async def enqueue(self, job: TicketJob) -> None:
        pool = await create_pool(redis_settings(), default_queue_name=QUEUE_NAME)
        try:
            # mode="json" so `received_at` crosses the wire as an ISO-8601
            # string rather than relying on arq's pickle serializer to
            # round-trip a datetime — the worker rebuilds the model with
            # `TicketJob.model_validate`.
            await pool.enqueue_job(
                RUN_TICKET_TASK,
                job.model_dump(mode="json"),
                _queue_name=QUEUE_NAME,
            )
        finally:
            await pool.aclose()


def get_job_queue() -> JobQueue:
    """FastAPI dependency. Every ingress test overrides this with an
    in-memory recorder; production gets the real Redis-backed queue."""
    return ArqJobQueue()
