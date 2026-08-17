"""The arq worker: consumes `cxforge:jobs` and runs the agent (ADR-002).

Container command (frozen with Track F): `arq worker.main.WorkerSettings`.

This module is the *only* place in `backend/src` that calls
`agent.graph.run_agent`. Before W1-A there was none, which is why 702 green
tests coexisted with a system that had never executed a single run outside a
test process (`docs/STATE.md §2`).

It also loads `.env` itself (W1-F4's other half). `backend/src/main.py` does
the same for the web process, but `arq worker.main.WorkerSettings` never
imports `main`, so without the block below the worker process — the one whose
entire job is to call Anthropic and Zendesk — would start with no credentials
at all. That is the same class of defect as `docs/STATE.md §6.14`: an
environment that looks configured and is not.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from arq import func
from arq.typing import WorkerSettingsBase
from dotenv import load_dotenv

# backend/src/worker/main.py -> worker -> src -> backend -> repo root.
#
# NOTE: this is `parents[3]`, not the `parents[2]` that is correct in
# `backend/src/main.py` — that file is one directory shallower. Derived from
# this file's own location rather than the working directory, because arq is
# launched from `/app` in the container and from the repo root by hand.
REPO_ROOT = Path(__file__).resolve().parents[3]


def _running_under_pytest() -> bool:
    """Same structural gate `backend/src/main.py` and `backend/src/data/db.py`
    use, for the same reason: importing this module inside a test must not
    push the developer's real `ANTHROPIC_API_KEY` and live Zendesk trial
    credentials into `os.environ` for the rest of the session. The ingress
    dispatch tests import `worker.main` directly, so without this gate the
    offline suite would stop being offline the moment they ran."""
    return "PYTEST_VERSION" in os.environ


if not _running_under_pytest():  # pragma: no cover - gated off inside the suite
    # override=False: anything already exported wins, so `docker compose`'s
    # `environment:` block can never be shadowed by a stray `.env` in the
    # image. Matches `main.load_repo_dotenv`'s precedence exactly.
    load_dotenv(REPO_ROOT / ".env", override=False)


from agent.graph import run_agent  # noqa: E402
from agent.llm import AnthropicLLMClient  # noqa: E402
from data import get_connection  # noqa: E402
from helpdesk.zendesk_adapter import ZendeskAdapter  # noqa: E402
from worker.jobs import TicketJob  # noqa: E402
from worker.settings import QUEUE_NAME, RUN_TICKET_TASK  # noqa: E402
from worker.settings import redis_settings as _redis_settings  # noqa: E402

logger = logging.getLogger(__name__)


def release_dedup_row(job: TicketJob) -> None:
    """ADR-003: a failed run releases its `(ticket_id, comment_id)` row from
    `tickets_seen`.

    The row is committed by ingress *before* dispatch, and the table carries
    only the two id columns, so without this a run that raises means that
    customer comment is dead forever — recoverable only by a manual DELETE.
    One transient Anthropic 529 during filming would silently kill a
    scenario. Releasing the row makes re-firing the Zendesk trigger a
    working recovery, with no migration and no status/attempts columns.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM tickets_seen WHERE ticket_id = %s AND comment_id = %s",
            (job.ticket_id, job.comment_id),
        )


async def run_ticket(ctx: dict[str, Any], payload: dict[str, Any]) -> None:
    """arq task `run_ticket`. One webhook event → one full agent turn.

    `run_agent` is synchronous (it makes blocking Anthropic, Zendesk and
    Postgres calls), so it runs in a worker thread rather than on the event
    loop. `asyncio.to_thread` resolves `run_agent` from this module's
    globals at call time, which is also what lets a test substitute it.

    The exception is **swallowed after** the row is released, not re-raised.
    Re-raising would let arq re-queue the job, and a second run against the
    same ticket posts a second public reply. Note carefully what does *not*
    protect us there: the `tickets_seen` row is **not** a guard on this
    function. Nothing in this module ever reads that table — the row only
    stops *ingress* from enqueueing the same event twice. So an arq retry is
    a duplicate reply whether or not the row was released. `max_tries=1` on
    the registration below is the only thing standing in the way; do not
    "restore" retries on the theory that the dedup row covers it.

    **Consequence to know about:** swallowing means arq books a failed run as
    a *completed* job — `success = True`, `jobs_complete` incremented,
    `jobs_failed` stays 0. `arq --check` and anything built on it will show a
    perfectly healthy worker while every run fails. The ERROR log below is
    the only honest signal, and it disagrees with every arq-derived metric.
    Do not fix that by re-raising (see above); fix it, if it matters, with a
    real health metric.
    """
    job = TicketJob.model_validate(payload)
    try:
        await asyncio.to_thread(
            run_agent,
            job.ticket_id,
            port=ZendeskAdapter(),
            llm=AnthropicLLMClient(),
            received_at=job.received_at,
        )
    except asyncio.CancelledError:
        # `CancelledError` is a BaseException, so the `except Exception`
        # below does NOT catch it — and this is the likeliest failure in the
        # system, not an exotic one. arq enforces `job_timeout` with
        # `asyncio.wait_for`, and a hung Anthropic call is exactly what runs
        # long: the SDK's default read timeout is 600s with 2 retries and
        # `agent/llm.py` overrides nothing, over 3+ model calls per run.
        # SIGTERM takes this path too — `docker compose restart worker`
        # mid-run, i.e. a redeploy during filming.
        #
        # Without this handler the row is never released, no ERROR is logged
        # (arq logs the timeout at WARNING), and the customer's comment is
        # marked seen forever with no run having completed — precisely the
        # state ADR-003 exists to prevent.
        #
        # Caveat that cannot be fixed here: `asyncio.to_thread` does not stop
        # the thread. `run_agent` keeps running after this handler returns and
        # may still post its reply — so releasing the row can result in a
        # reprocessed ticket whose first run also eventually replied. Bounding
        # the model call is the real fix and it belongs in `agent/llm.py`,
        # which W2-C owns; `job_timeout` below is the blunt instrument until
        # then. Re-raised, as `CancelledError` always must be.
        try:
            release_dedup_row(job)
        except Exception:  # pragma: no cover - only when Postgres is also down
            logger.exception(
                "could not release tickets_seen row for ticket %s comment %s "
                "after cancellation",
                job.ticket_id,
                job.comment_id,
            )
        logger.error(
            "agent run cancelled for ticket %s (comment %s) — job_timeout or "
            "shutdown; the worker thread may still be running",
            job.ticket_id,
            job.comment_id,
        )
        raise
    except Exception as exc:
        try:
            release_dedup_row(job)
        except Exception:  # pragma: no cover - only when Postgres is also down
            logger.exception(
                "could not release tickets_seen row for ticket %s comment %s",
                job.ticket_id,
                job.comment_id,
            )
        logger.error(
            "agent run failed for ticket %s (comment %s): %r",
            job.ticket_id,
            job.comment_id,
            exc,
            exc_info=True,
        )


# arq's default is 300s. A single run makes 3+ Anthropic calls, and the SDK's
# default read timeout is 600s with 2 retries, so the default cancels healthy
# runs. This is a ceiling, not a correct bound — the honest fix is an explicit
# client-side timeout in `agent/llm.py` (W2-C's file), after which this should
# come down. Recorded rather than silently left at the default, because a
# cancellation here is a released dedup row plus a thread still running.
JOB_TIMEOUT_SECONDS = 900


class WorkerSettings(WorkerSettingsBase):
    """arq entrypoint. `arq worker.main.WorkerSettings` reads every public
    attribute here as a `Worker(...)` kwarg — via `settings_cls.__dict__`
    (`arq.worker.get_kwargs`), so every value must be a concrete object sitting
    in this class body. arq will not call a callable or resolve a descriptor,
    which is why `redis_settings` below is a snapshot rather than a function.

    Inheriting arq's own `WorkerSettingsBase` protocol is deliberate: it makes
    mypy check the shape Track F's container command depends on, instead of
    leaving `arq worker.main.WorkerSettings` to fail at runtime on the
    droplet. Only names in *this* class body land in `__dict__`, so inherited
    defaults stay out of arq's kwargs.
    """

    functions = [func(run_ticket, name=RUN_TICKET_TASK, max_tries=1)]
    queue_name = QUEUE_NAME
    job_timeout = JOB_TIMEOUT_SECONDS

    # Evaluated once, at import. This is the one place `REDIS_URL` is NOT read
    # at call time (`worker.settings.redis_url`'s contract), because arq
    # requires a concrete `RedisSettings` here. It is correct only because the
    # `load_dotenv()` call at the top of this module runs *before* this class
    # body — do not move either past the other. `test_queue_contract.py` pins
    # the resulting behaviour with a subprocess.
    redis_settings = _redis_settings()
