"""W1-A: the webhook actually starts an agent run (ADR-002/003/004).

Until 2026-08-16 `backend/src` contained **zero** calls to
`agent.graph.run_agent` — 15 invocations repo-wide, every one of them under
`backend/tests/` — so the webhook validated, deduped, returned 202, and
nothing ever happened (`docs/STATE.md §1–2`). 702 tests were green
throughout. These tests exist so that cannot recur silently: each one is
written to go red if a specific link in the chain is removed, and each was
confirmed red by actually removing it (see the sabotage log in the W1-A
report, not merely reasoned about).

Chain under test:

    POST /webhooks/zendesk → tickets_seen INSERT → TicketJob on cxforge:jobs
                           → worker.main.run_ticket → run_agent → runs row

Everything here is real except the two external services (Zendesk,
Anthropic) and the Redis hop: real FastAPI app, real signature
verification, real Postgres, real LangGraph pipeline, real
`agent.store.record_run`. The signing helpers are reused from
`test_webhook.py`, which computes them independently of
`ingress.signature` on purpose (see that module's docstring); a third
reimplementation here would add nothing.

Placed in `backend/tests/ingress/` rather than a new
`backend/tests/worker/`: the latter turns 4–11 closed tickets red in
`backend/tests/plan/test_blast_radius.py` (`docs/BUILD-PLAN.md §3`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

import ingress
from agent.graph import run_agent
from agent.llm import AnthropicLLMClient
from agent.schemas import Classification
from data import get_connection
from escalation.schemas import EscalationCall
from helpdesk.models import EscalationGroup, Message, MessageRef, Ticket, TicketStatus
from helpdesk.zendesk_adapter import ZendeskAdapter
from main import app
from worker import main as worker_main
from worker.jobs import TicketJob
from worker.queue import get_job_queue

from .conftest import RecordingJobQueue
from .test_webhook import URL, _headers, _payload_bytes, _seen_count

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_DB_TESTS") == "1",
    reason="requires the docker-compose db service (CI sets SKIP_DB_TESTS=1)",
)

# The response bytes the endpoint produced BEFORE W1-A wired dispatch in.
# Captured from `git show HEAD:backend/src/ingress/__init__.py`, whose two
# return statements are character-for-character the ones the handler still
# uses; FastAPI serializes them with `separators=(",", ":")` and no
# response_model. DESIGN §1.1: "the endpoint keeps status_code=202 and its
# existing response body unchanged — a failed run never changes what
# Zendesk sees."
ACCEPTED_BODY = b'{"status":"accepted","duplicate":false}'
DUPLICATE_BODY = b'{"status":"accepted","duplicate":true}'
DROPPED_BODY = b'{"status":"dropped","reason":"self-authored","duplicate":false}'

# The one new response shape W1-A adds (ADR-017). Fixed text, no exception
# interpolated: a broker error carries the Redis DSN, and in a misconfigured
# deployment a DSN carries a password.
BROKER_FAILURE_BODY = b'{"detail":"could not dispatch the event for processing"}'

# Two deliberately-visible delays used by the latency tests. `INGEST_DELAY`
# sits in the FIRST port call of the run (`fetch_ticket`, called by
# `ingest`); `QUEUE_LATENCY` sits between the webhook and the worker. Both
# are invisible to the pre-ADR-004 clock, which was minted inside `act` —
# the last node — and therefore timed only the port calls below it.
INGEST_DELAY = timedelta(milliseconds=300)
QUEUE_LATENCY = timedelta(milliseconds=400)


# --------------------------------------------------------------------------
# Test doubles for the two external services only.
# --------------------------------------------------------------------------


@dataclass
class _RecordingPort:
    """In-memory `HelpdeskPort` for one ticket.

    `ingest_delay` makes the run take measurable wall-clock time *before*
    `act` is reached, which is the whole difference between "latency" as
    DESIGN defines it and what the code measured before ADR-004.
    """

    ticket_id: str
    requester_email: str = "customer@example.com"
    message: str = "Do you sell forensic t-shirts?"
    ingest_delay: timedelta = timedelta(0)
    public_replies: list[str] = field(default_factory=list)
    internal_notes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)

    def fetch_ticket(self, ticket_id: str) -> Ticket:
        time.sleep(self.ingest_delay.total_seconds())
        return Ticket(
            id=ticket_id,
            subject="hello",
            requester_email=self.requester_email,
            status="open",
            tags=[],
            created_at=datetime(2026, 8, 16, tzinfo=UTC),
        )

    def fetch_conversation(self, ticket_id: str) -> list[Message]:
        return [
            Message(
                id="m-1",
                author_kind="customer",
                text=self.message,
                public=True,
                created_at=datetime(2026, 8, 16, tzinfo=UTC),
            )
        ]

    def post_public_reply(self, ticket_id: str, html_body: str) -> MessageRef:
        self.public_replies.append(html_body)
        return MessageRef(ticket_id=ticket_id, message_id="reply-1", public=True)

    def post_internal_note(self, ticket_id: str, body: str) -> MessageRef:
        self.internal_notes.append(body)
        return MessageRef(ticket_id=ticket_id, message_id="note-1", public=False)

    def add_tags(self, ticket_id: str, tags: list[str]) -> None:
        self.tags.extend(tags)

    def set_status(self, ticket_id: str, status: TicketStatus) -> None:
        self.statuses.append(status)

    def assign_group(self, ticket_id: str, group: EscalationGroup) -> None:
        pass


@dataclass
class _FakeLLM:
    """Minimal `agent.llm.LLMClient`. Mirrors
    `backend/tests/graph/fakes.py::FakeLLMClient`'s contract (including its
    non-escalating `EscalationCall` default) rather than importing it —
    cross-suite-directory imports are not resolvable under
    `--import-mode=importlib` without `__init__.py` files."""

    responses: dict[type[BaseModel], BaseModel]

    def __post_init__(self) -> None:
        self.responses.setdefault(
            EscalationCall, EscalationCall(escalate=False, reasons=[], confidence=0.0)
        )

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        response = self.responses.get(schema)
        if response is None:
            raise AssertionError(f"_FakeLLM has no canned response for {schema.__name__}")
        return response


def _off_topic_llm() -> _FakeLLM:
    """The `off_topic` route reaches `act` without touching case data or the
    KB, so the whole pipeline runs against Postgres with nothing seeded."""
    return _FakeLLM(
        responses={
            Classification: Classification(
                topic="unrelated merchandise question",
                route="off_topic",
                case_id=None,
                confidence=0.93,
            )
        }
    )


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def zendesk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`worker.main.run_ticket` builds a real `ZendeskAdapter()`, which
    raises `HelpdeskConfigError` without these. Setting them keeps a missing
    credential from masquerading as the failure a test meant to induce."""
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "cxforge-dispatch-test")
    monkeypatch.setenv("ZENDESK_OAUTH_TOKEN", "not-a-real-oauth-token")


@pytest.fixture
def runs_cleanup(_schema_ready: None) -> Iterator[None]:
    """Remove any `runs`/`drafts` rows this module's real graph runs wrote."""
    yield
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM drafts WHERE run_id IN "
            "(SELECT id FROM runs WHERE ticket_id LIKE 'ingress-test-%')"
        )
        cur.execute("DELETE FROM runs WHERE ticket_id LIKE 'ingress-test-%'")


def _run_rows(ticket_id: str) -> list[tuple[datetime | None, datetime | None, str | None]]:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT received_at, replied_at, outcome FROM runs WHERE ticket_id = %s",
            (ticket_id,),
        )
        return [(row[0], row[1], row[2]) for row in cur.fetchall()]


# --------------------------------------------------------------------------
# A7.1 — a valid webhook enqueues, and does not run the agent in-process.
# --------------------------------------------------------------------------


def test_valid_webhook_enqueues_a_ticket_job(
    client: TestClient, ticket_id: str, job_queue: RecordingJobQueue
) -> None:
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")

    before = datetime.now(UTC)
    response = client.post(URL, content=body, headers=_headers(body))
    after = datetime.now(UTC)

    assert response.status_code == 202
    assert len(job_queue.enqueued) == 1, (
        "the webhook accepted the event but enqueued nothing — this is exactly "
        "the severed core loop docs/STATE.md §2 describes"
    )
    job = job_queue.enqueued[0]
    assert isinstance(job, TicketJob)
    assert job.ticket_id == ticket_id
    assert job.comment_id == "c-1"
    # Stamped inside the handler, not by the worker or the test.
    assert before <= job.received_at <= after


def test_webhook_does_not_run_the_agent_in_process(
    client: TestClient,
    ticket_id: str,
    job_queue: RecordingJobQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatch, not inline execution: holding Zendesk's connection open
    through 20–60s of model calls would serialize all intake (ADR-002).

    Tripwires are installed on both name bindings of `run_agent` — the
    definition in `agent.graph` and `worker.main`'s import of it — because
    patching only the definition site would miss a caller that had already
    bound the function object at import time.
    """
    import agent.graph as agent_graph

    calls: list[str] = []

    def _tripwire(*args: Any, **kwargs: Any) -> Any:
        calls.append("run_agent")
        raise AssertionError("run_agent was called inside the request handler")

    monkeypatch.setattr(agent_graph, "run_agent", _tripwire)
    monkeypatch.setattr(worker_main, "run_agent", _tripwire)

    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")
    response = client.post(URL, content=body, headers=_headers(body))

    assert response.status_code == 202
    assert calls == []
    # The observable consequence of an inline run would be a `runs` row.
    assert _run_rows(ticket_id) == []
    # ...and the work is not lost: it went to the queue instead.
    assert len(job_queue.enqueued) == 1


# --------------------------------------------------------------------------
# A7.6 — a duplicate enqueues nothing; a dropped self-event enqueues nothing.
# --------------------------------------------------------------------------


def test_duplicate_webhook_enqueues_nothing(
    client: TestClient, ticket_id: str, job_queue: RecordingJobQueue
) -> None:
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")

    first = client.post(URL, content=body, headers=_headers(body))
    second = client.post(URL, content=body, headers=_headers(body))

    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert len(job_queue.enqueued) == 1, (
        "a redelivered Zendesk event enqueued a second run — the loop guard "
        "is in tickets_seen, and dispatch must sit behind it"
    )
    assert job_queue.enqueued[0].comment_id == "c-1"


def test_self_authored_event_enqueues_nothing(
    client: TestClient, ticket_id: str, job_queue: RecordingJobQueue
) -> None:
    """The AI's own comment must not start a run — loop-guard line two."""
    from .conftest import TEST_AI_USER_ID

    body = _payload_bytes(
        ticket_id=ticket_id, comment_id="c-1", comment_author_id=TEST_AI_USER_ID
    )

    response = client.post(URL, content=body, headers=_headers(body))

    assert response.status_code == 202
    assert response.json()["status"] == "dropped"
    assert job_queue.enqueued == []


def test_rejected_webhook_enqueues_nothing(
    client: TestClient, ticket_id: str, job_queue: RecordingJobQueue
) -> None:
    """An unsigned request is rejected before anything is dispatched — the
    signature is a hard gate on every later step, dispatch included."""
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")

    response = client.post(URL, content=body, headers={"Content-Type": "application/json"})

    assert 400 <= response.status_code < 500
    assert job_queue.enqueued == []


# --------------------------------------------------------------------------
# A7.4 — the 202 contract and the response body are byte-identical.
# --------------------------------------------------------------------------


def test_response_bodies_are_byte_identical_to_pre_dispatch(
    client: TestClient, ticket_id: str
) -> None:
    from .conftest import TEST_AI_USER_ID

    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")
    first = client.post(URL, content=body, headers=_headers(body))
    second = client.post(URL, content=body, headers=_headers(body))

    dropped_body = _payload_bytes(
        ticket_id=ticket_id, comment_id="c-2", comment_author_id=TEST_AI_USER_ID
    )
    dropped = client.post(URL, content=dropped_body, headers=_headers(dropped_body))

    assert (first.status_code, first.content) == (202, ACCEPTED_BODY)
    assert (second.status_code, second.content) == (202, DUPLICATE_BODY)
    assert (dropped.status_code, dropped.content) == (202, DROPPED_BODY)


def test_a_broken_broker_returns_500_so_zendesk_retries(
    client: TestClient,
    ticket_id: str,
    job_queue: RecordingJobQueue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ADR-017. Redis unreachable is a failed **dispatch**, not a failed run:
    the work was never accepted, so 202 ("we have it") would be a lie that
    costs the customer their event — Zendesk does not retry a 202.

    Three things must hold together, and each is load-bearing:
      * 500, so the retry happens at all;
      * the dedup row released, or that retry is swallowed as a duplicate and
        the 500 buys nothing;
      * the exception on the ERROR log and *not* in the response body, which
        goes out over a public endpoint and must not carry a Redis DSN.
    """
    job_queue.fail_with = RuntimeError("redis connection refused at redis://:hunter2@10.0.0.4")
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")

    with caplog.at_level(logging.ERROR, logger="ingress"):
        response = client.post(URL, content=body, headers=_headers(body))

    assert response.status_code == 500, (
        "a dropped dispatch answered 202 — Zendesk will not retry, so the "
        "customer's event is gone with only an ERROR log as evidence (ADR-017)"
    )
    # Checked before the exact-body comparison so a leak names itself rather
    # than showing up as a truncated bytes diff.
    decoded = response.content.decode()
    for leak in ("redis", "hunter2", "10.0.0.4", "RuntimeError", "Traceback"):
        assert leak not in decoded, (
            f"{leak!r} leaked into the webhook response body: {decoded!r}"
        )
    assert response.content == BROKER_FAILURE_BODY

    assert _seen_count(ticket_id, "c-1") == 0, (
        "the event stayed marked as seen after a failed dispatch — Zendesk's "
        "retry will now be swallowed as a duplicate, defeating the 500"
    )

    messages = [record.getMessage() for record in caplog.records]
    assert any(ticket_id in m and "redis connection refused" in m for m in messages), messages


def test_a_transient_blip_is_absorbed_and_never_reaches_zendesk(
    client: TestClient,
    ticket_id: str,
    job_queue: RecordingJobQueue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ADR-017's 500 buys a Zendesk retry, but **Zendesk deactivates a webhook
    after sustained consecutive failures** — so a long broker outage could
    disable the endpoint entirely, which is worse than losing one comment. Two
    in-handler attempts absorb the overwhelmingly likely case (a blip against
    this request's own short-lived pool) so it never reaches Zendesk as a 5xx.

    A recovered blip must be indistinguishable from a clean first attempt.
    """
    job_queue.fail_with = RuntimeError("redis connection reset")
    job_queue.fail_times = 1
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")

    with caplog.at_level(logging.WARNING, logger="ingress"):
        response = client.post(URL, content=body, headers=_headers(body))

    # Indistinguishable from a clean first attempt, byte for byte.
    assert (response.status_code, response.content) == (202, ACCEPTED_BODY)

    # Two attempts, but exactly ONE job queued — a retry loop that re-queued on
    # success would run the agent twice and post two public replies.
    assert len(job_queue.attempts) == 2
    assert len(job_queue.enqueued) == 1, (
        f"the retry queued the job more than once: {job_queue.enqueued}"
    )

    # The dedup row is NOT released on a recovered blip: the event was accepted.
    assert _seen_count(ticket_id, "c-1") == 1

    # ADR-004: a retry must not re-stamp the clock. Both attempts, and the job
    # that actually landed, carry the one webhook-receipt time.
    assert job_queue.attempts[0].received_at == job_queue.attempts[1].received_at
    assert job_queue.enqueued[0].received_at == job_queue.attempts[0].received_at

    # WARNING, not ERROR: ADR-003 makes the ERROR stream the only signal that a
    # run failed, so a blip that recovered must not pollute it.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(ticket_id in r.getMessage() for r in warnings), [
        r.getMessage() for r in caplog.records
    ]
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_a_dead_broker_is_retried_exactly_twice_and_bounded(
    client: TestClient,
    ticket_id: str,
    job_queue: RecordingJobQueue,
) -> None:
    """The attempt count is asserted as a literal, so changing the constant
    forces a deliberate test edit rather than silently widening the loop.

    The elapsed-time bound matters as much as the count: this delay is spent
    holding Zendesk's connection open, so an "improvement" to a 30-second
    exponential backoff would trade a rare lost comment for a routinely
    timed-out webhook.
    """
    job_queue.fail_with = RuntimeError("redis connection refused")
    job_queue.fail_times = None  # genuinely down, every attempt fails
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")

    started = time.monotonic()
    response = client.post(URL, content=body, headers=_headers(body))
    elapsed = time.monotonic() - started

    assert response.status_code == 500
    assert len(job_queue.attempts) == 2, (
        f"expected exactly 2 enqueue attempts, saw {len(job_queue.attempts)}"
    )
    assert job_queue.enqueued == []
    assert _seen_count(ticket_id, "c-1") == 0

    # Cross-check the literal against the constant, so the two cannot drift.
    assert ingress.ENQUEUE_ATTEMPTS == 2

    # A retry actually happened (the delay was paid) but stayed well bounded.
    assert elapsed >= ingress.ENQUEUE_RETRY_DELAY_SECONDS * 0.75, (
        f"the failure path returned in {elapsed:.3f}s — faster than one retry "
        "delay, so no retry was attempted"
    )
    assert elapsed < 1.0, (
        f"the failure path held Zendesk's connection for {elapsed:.3f}s; this "
        "must stay well under a second"
    )


class _HangingJobQueue:
    """Blocks inside `enqueue` so the handler can be cancelled mid-await."""

    def __init__(self, hold_seconds: float) -> None:
        self.hold_seconds = hold_seconds
        self.entered = asyncio.Event()

    async def enqueue(self, job: TicketJob) -> None:
        self.entered.set()
        await asyncio.sleep(self.hold_seconds)


async def _post_through_asgi(body: bytes, *, timeout: float) -> httpx.Response:
    """Drive one webhook through the real ASGI app and cancel it in flight.

    `httpx.ASGITransport` awaits the app coroutine in *this* task, so
    `asyncio.wait_for` cancelling here cancels the handler exactly the way
    uvicorn does on client disconnect or graceful shutdown. Cancelling a
    `TestClient` call could not reproduce that — it runs the loop on another
    thread.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://ingress-test"
    ) as async_client:
        return await asyncio.wait_for(
            async_client.post(URL, content=body, headers=_headers(body)), timeout=timeout
        )


def test_a_cancelled_enqueue_releases_the_dedup_row(
    ticket_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    """N1 — the ingress half of the `CancelledError` asymmetry.

    uvicorn cancels the handler task on client disconnect and on graceful
    shutdown. `CancelledError` is a `BaseException`, so the `except Exception`
    around the enqueue does not catch it. Uncaught, the sequence is: row
    inserted → never enqueued → never released → no response; Zendesk retries,
    hits the surviving row, gets `duplicate: true` and a 202, and **nothing
    ever runs**. That is the dead-comment state ADR-003 and ADR-017 both exist
    to prevent, reached from the ingress side instead of the worker side.
    """
    queue = _HangingJobQueue(hold_seconds=5.0)
    app.dependency_overrides[get_job_queue] = lambda: queue
    try:
        body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")

        with caplog.at_level(logging.ERROR, logger="ingress"):
            with pytest.raises(TimeoutError):
                asyncio.run(_post_through_asgi(body, timeout=0.3))
    finally:
        app.dependency_overrides.pop(get_job_queue, None)

    assert queue.entered.is_set(), "the handler never reached the enqueue"
    assert _seen_count(ticket_id, "c-1") == 0, (
        "a cancelled enqueue left its dedup row behind — Zendesk's retry will "
        "get duplicate:true and 202, and the comment is dead forever"
    )
    messages = [r.getMessage() for r in caplog.records]
    assert any(ticket_id in m and "cancelled" in m for m in messages), messages


def test_a_cancellation_between_retry_attempts_releases_the_dedup_row(
    ticket_id: str, job_queue: RecordingJobQueue, caplog: pytest.LogCaptureFixture
) -> None:
    """The retry loop must not swallow a cancellation that lands in its sleep.

    Attempt 1 fails immediately, then the handler is inside
    `asyncio.sleep(ENQUEUE_RETRY_DELAY_SECONDS)` when cancellation arrives — a
    different code path from the test above, and one the retry work introduced.
    It must release the row and re-raise, never be converted into a 500.
    """
    job_queue.fail_with = RuntimeError("redis connection refused")
    app.dependency_overrides[get_job_queue] = lambda: job_queue
    try:
        body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")

        with caplog.at_level(logging.ERROR, logger="ingress"):
            with pytest.raises(TimeoutError):
                # Shorter than the retry delay, so the cancel lands in the sleep.
                asyncio.run(
                    _post_through_asgi(
                        body, timeout=ingress.ENQUEUE_RETRY_DELAY_SECONDS / 2
                    )
                )
    finally:
        app.dependency_overrides.pop(get_job_queue, None)

    assert len(job_queue.attempts) == 1, (
        f"expected cancellation during the retry sleep, after exactly one "
        f"attempt; saw {len(job_queue.attempts)}"
    )
    assert _seen_count(ticket_id, "c-1") == 0
    messages = [r.getMessage() for r in caplog.records]
    assert any(ticket_id in m and "cancelled" in m for m in messages), messages


def test_after_a_broken_broker_the_retry_is_treated_as_new(
    client: TestClient, ticket_id: str, job_queue: RecordingJobQueue
) -> None:
    """The point of releasing the row on a 500: Zendesk's retry of the exact
    same event must reach the queue, not be deduped away."""
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")

    job_queue.fail_with = RuntimeError("redis connection refused")
    failed = client.post(URL, content=body, headers=_headers(body))
    assert failed.status_code == 500
    assert job_queue.enqueued == []

    # Redis comes back; Zendesk redelivers the identical event.
    job_queue.fail_with = None
    retried = client.post(URL, content=body, headers=_headers(body))

    assert (retried.status_code, retried.content) == (202, ACCEPTED_BODY)
    assert retried.json()["duplicate"] is False
    assert len(job_queue.enqueued) == 1
    assert job_queue.enqueued[0].ticket_id == ticket_id
    assert job_queue.enqueued[0].comment_id == "c-1"
    assert _seen_count(ticket_id, "c-1") == 1


# --------------------------------------------------------------------------
# A7.2 — the worker runs the agent with the job's clock, not a fresh one.
# --------------------------------------------------------------------------


def test_worker_calls_run_agent_with_the_jobs_received_at(
    zendesk_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[dict[str, Any]] = []

    def _record(ticket_id: str, **kwargs: Any) -> dict[str, Any]:
        recorded.append({"ticket_id": ticket_id, **kwargs})
        return {}

    monkeypatch.setattr(worker_main, "run_agent", _record)

    # Deliberately not "now": a fresh clock inside the run would not match.
    stamped = datetime(2026, 8, 16, 9, 30, 0, 500000, tzinfo=UTC)
    job = TicketJob(ticket_id="ticket-77", comment_id="c-9", received_at=stamped)

    asyncio.run(worker_main.run_ticket({}, job.model_dump(mode="json")))

    assert len(recorded) == 1, (
        "the worker consumed the job without calling run_agent (an exception "
        "on the way would have been swallowed by ADR-003's handler)"
    )
    call = recorded[0]
    assert call["ticket_id"] == "ticket-77"
    assert call["received_at"] == stamped
    # A3: the worker builds the real collaborators, not stand-ins.
    assert isinstance(call["port"], ZendeskAdapter)
    assert isinstance(call["llm"], AnthropicLLMClient)


# --------------------------------------------------------------------------
# A7.3 — a run that raises releases its dedup row.
# --------------------------------------------------------------------------


def test_failed_run_releases_the_row_and_the_next_webhook_is_new(
    client: TestClient,
    ticket_id: str,
    job_queue: RecordingJobQueue,
    zendesk_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")
    first = client.post(URL, content=body, headers=_headers(body))

    assert first.json()["duplicate"] is False
    assert _seen_count(ticket_id, "c-1") == 1
    (job,) = job_queue.enqueued

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("anthropic overloaded_error 529")

    monkeypatch.setattr(worker_main, "run_agent", _boom)

    with caplog.at_level(logging.ERROR, logger="worker.main"):
        asyncio.run(worker_main.run_ticket({}, job.model_dump(mode="json")))

    assert _seen_count(ticket_id, "c-1") == 0, (
        "the failed run left its dedup row behind — that customer comment is "
        "now unprocessable without a manual DELETE (ADR-003)"
    )
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        ticket_id in m and "overloaded_error 529" in m for m in messages
    ), messages

    # ...and the identical redelivery is treated as new, which is the whole
    # point of releasing the row.
    job_queue.enqueued.clear()
    second = client.post(URL, content=body, headers=_headers(body))

    assert second.status_code == 202
    assert second.json()["duplicate"] is False
    assert len(job_queue.enqueued) == 1
    assert job_queue.enqueued[0].received_at > job.received_at


def test_a_cancelled_run_releases_the_dedup_row(
    client: TestClient,
    ticket_id: str,
    job_queue: RecordingJobQueue,
    zendesk_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`asyncio.CancelledError` is a `BaseException`, so a bare
    `except Exception` misses it — and this is the *likeliest* failure here,
    not an exotic one.

    arq enforces `job_timeout` with `asyncio.wait_for`, and a hung Anthropic
    call is exactly what runs long: the SDK's default read timeout is 600s
    with 2 retries and `agent/llm.py` overrides nothing, across 3+ model calls
    per run. SIGTERM (`docker compose restart worker` mid-run) takes the same
    path. Uncaught, the row is never released, arq logs only at WARNING, and
    the customer's comment is marked seen forever with no run ever completing
    — the exact state ADR-003 exists to prevent.

    The cancellation is driven the way arq itself drives it (`wait_for` around
    the task), not by raising `CancelledError` from a stub, so this fails if
    the handler stops covering the real mechanism.
    """
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")
    client.post(URL, content=body, headers=_headers(body))
    assert _seen_count(ticket_id, "c-1") == 1
    (job,) = job_queue.enqueued

    entered = threading.Event()

    def _hang(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        # Outlives the wait_for below. `asyncio.to_thread` cannot interrupt
        # this — the thread keeps running after arq has given up on the job.
        time.sleep(1.0)

    monkeypatch.setattr(worker_main, "run_agent", _hang)

    async def _drive_with_arq_job_timeout() -> None:
        await asyncio.wait_for(
            worker_main.run_ticket({}, job.model_dump(mode="json")), timeout=0.2
        )

    with caplog.at_level(logging.ERROR, logger="worker.main"):
        with pytest.raises(TimeoutError):
            asyncio.run(_drive_with_arq_job_timeout())

    assert entered.is_set(), "the run never started, so nothing was cancelled"
    assert _seen_count(ticket_id, "c-1") == 0, (
        "a cancelled run left its dedup row behind — the comment is now "
        "unprocessable forever and arq logged only a WARNING (ADR-003)"
    )
    messages = [record.getMessage() for record in caplog.records]
    assert any(ticket_id in m and "cancelled" in m for m in messages), messages


# --------------------------------------------------------------------------
# A7.5 — runs.received_at is the webhook stamp, and the interval spans the
# whole run rather than the tail-end port calls.
# --------------------------------------------------------------------------


def test_runs_received_at_is_the_webhook_stamp_and_spans_the_whole_run(
    client: TestClient,
    ticket_id: str,
    job_queue: RecordingJobQueue,
    runs_cleanup: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: webhook → job → worker → real `run_agent` → `runs` row.

    Only Zendesk and Anthropic are faked. The graph, `agent.store.record_run`
    and Postgres are real, so `runs.received_at` here is the same column
    `/api/metrics` reads for R8/R13.
    """
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")
    response = client.post(URL, content=body, headers=_headers(body))
    assert response.status_code == 202
    (job,) = job_queue.enqueued

    # Time the job spends waiting for a worker is part of "webhook receipt →
    # public reply posted" and was previously invisible.
    time.sleep(QUEUE_LATENCY.total_seconds())

    port = _RecordingPort(ticket_id=ticket_id, ingest_delay=INGEST_DELAY)
    monkeypatch.setattr(worker_main, "ZendeskAdapter", lambda: port)
    monkeypatch.setattr(worker_main, "AnthropicLLMClient", _off_topic_llm)

    asyncio.run(worker_main.run_ticket({}, job.model_dump(mode="json")))

    assert port.public_replies, "the run never reached the port — nothing was sent"

    rows = _run_rows(ticket_id)
    assert len(rows) == 1, f"expected exactly one run row, got {rows}"
    received_at, replied_at, outcome = rows[0]
    assert outcome == "off_topic"
    assert received_at == job.received_at, (
        "runs.received_at is not the webhook stamp — act minted its own clock"
    )
    assert replied_at is not None
    assert replied_at - received_at >= QUEUE_LATENCY + INGEST_DELAY, (
        "the recorded interval does not span the queue wait and the ingest "
        "port call, so it is still measuring only act's own port calls "
        "(docs/STATE.md §4.1)"
    )


def test_without_an_injected_stamp_the_interval_only_covers_act(
    ticket_id: str, runs_cleanup: None
) -> None:
    """The control that makes the test above mean something.

    This is the pre-ADR-004 behaviour, still reachable through the preserved
    `received_at=None` fallback: the clock is minted inside `act`, so the
    interval excludes ingest, classify, retrieval, compose, verify and
    decide — here, demonstrably, it excludes `INGEST_DELAY`. Every existing
    graph/grounding/escalation test runs on this path unchanged.
    """
    port = _RecordingPort(ticket_id=ticket_id, ingest_delay=INGEST_DELAY)

    started = time.monotonic()
    run_agent(ticket_id, port=port, llm=_off_topic_llm())
    wall_clock = timedelta(seconds=time.monotonic() - started)

    # Without this the control passes vacuously: if `ingest` ever stopped
    # calling `fetch_ticket`, INGEST_DELAY would never elapse and "the
    # interval is short" would be trivially true.
    assert port.public_replies, "the run never reached the port — nothing was sent"
    assert wall_clock >= INGEST_DELAY, (
        f"the run finished in {wall_clock}, faster than the ingest delay it "
        "was supposed to incur — the delay is not being exercised"
    )

    rows = _run_rows(ticket_id)
    assert len(rows) == 1
    received_at, replied_at, _ = rows[0]
    assert received_at is not None and replied_at is not None
    # Differential, not a magic constant: the recorded interval must be a
    # small fraction of what the run demonstrably took.
    assert replied_at - received_at < wall_clock / 2, (
        f"the fallback clock spans {replied_at - received_at} of a {wall_clock} "
        "run, so it is no longer measuring only act; if act stopped minting its "
        "own time this control is obsolete rather than failing"
    )
