"""Shared fixtures for the webhook ingress test suite (DESIGN §Webhook
ingress, SPEC R1).

Every test here posts through the real FastAPI app (`main.app`) and the
real docker-compose Postgres `tickets_seen` table — there is no mock of
either. This mirrors `backend/tests/data/conftest.py`'s
`SKIP_DB_TESTS` convention: test_webhook.py sets its own module-level
`pytestmark` skipif so the whole file no-ops in CI (which has no db
service), rather than this conftest special-casing anything.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from data import get_connection, init_schema
from main import app
from worker.jobs import TicketJob
from worker.queue import get_job_queue

# Shaped like a REAL Zendesk signing secret: a 44-character opaque string
# that is deliberately NOT valid base64.
#
# The previous fixture was `base64.b64encode(...)`, which let a bug survive
# to production: the implementation base64-decoded the secret before using
# it as the HMAC key, so every genuine Zendesk request failed closed with
# 401 "signing secret is not valid base64" — while this suite stayed green,
# because a base64-derived fixture decodes happily. A fixture that cannot
# be decoded keeps the test honest about what Zendesk actually sends.
TEST_SIGNING_SECRET = "zEnDeskT3stSigningS3cret_notBase64_44charsXY"
TEST_AI_USER_ID = "ingress-test-ai-user-999"


@pytest.fixture(autouse=True)
def _ingress_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ZENDESK_WEBHOOK_SIGNING_SECRET / ZENDESK_AI_USER_ID for every
    test, overriding (and restoring, via monkeypatch) whatever the host
    environment has."""
    monkeypatch.setenv("ZENDESK_WEBHOOK_SIGNING_SECRET", TEST_SIGNING_SECRET)
    monkeypatch.setenv("ZENDESK_AI_USER_ID", TEST_AI_USER_ID)


@pytest.fixture(scope="session")
def _schema_ready() -> None:
    """tickets_seen already exists (T-1's init_schema), but this suite must
    not assume it's run after T-1's tests against a fresh database, so make
    the schema present itself — init_schema is idempotent (CREATE TABLE IF
    NOT EXISTS), so this is always safe to call."""
    with get_connection() as conn:
        init_schema(conn)


@dataclass
class RecordingJobQueue:
    """In-memory stand-in for `worker.queue.ArqJobQueue` (W1-A / ADR-002).

    Records every `TicketJob` the handler enqueues, so a test can assert on
    *the queue itself* rather than on a return value the handler could
    fabricate. `fail_with` drives the broker-unreachable path.

    Substituted through `app.dependency_overrides`, exactly as the portal
    suite substitutes `get_helpdesk_port` — which means deleting the
    `await queue.enqueue(...)` line from the handler leaves `enqueued`
    empty and fails `test_valid_webhook_enqueues_a_ticket_job`. That is the
    sabotage check `docs/BUILD-PLAN.md §3 Track A` requires.

    `attempts` records **every** call, including ones that raised; `enqueued`
    records only the calls that succeeded. Keeping them separate is what lets a
    test tell "the retry worked" (2 attempts, 1 job) apart from "the handler
    queued the job twice" (2 attempts, 2 jobs) — the failure mode a retry loop
    introduces and the one a single counter would hide.

    `fail_times` bounds the failure to the first N attempts, modelling the
    transient blip ADR-017's in-handler retry exists to absorb. `None` means
    fail for as long as `fail_with` is set (a broker that is genuinely down).
    """

    enqueued: list[TicketJob] = field(default_factory=list)
    attempts: list[TicketJob] = field(default_factory=list)
    fail_with: Exception | None = None
    fail_times: int | None = None

    async def enqueue(self, job: TicketJob) -> None:
        self.attempts.append(job)
        if self.fail_with is not None and (
            self.fail_times is None or len(self.attempts) <= self.fail_times
        ):
            raise self.fail_with
        self.enqueued.append(job)


@pytest.fixture
def job_queue() -> Iterator[RecordingJobQueue]:
    """Deliberately **not** autouse. `client` depends on it, so every test
    that posts a webhook — including the six pre-existing `test_webhook.py`
    tests whose `202` assertions DESIGN §1.7 pins — already gets the
    substitution. Autouse would add no coverage and would guarantee that a
    future test written to exercise the *production* `ArqJobQueue` silently
    received this fake instead. Tests that care about dispatch request the
    fixture by name and read `.enqueued`."""
    queue = RecordingJobQueue()
    app.dependency_overrides[get_job_queue] = lambda: queue
    yield queue
    app.dependency_overrides.pop(get_job_queue, None)


@pytest.fixture
def client(_schema_ready: None, job_queue: RecordingJobQueue) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def unique_ticket_id() -> str:
    """A ticket_id namespaced to this test run, so cleanup can target
    exactly the rows a test created without disturbing any other suite's
    (or a prior failed run's leftover) data."""
    return f"ingress-test-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def ticket_id(_schema_ready: None) -> Iterator[str]:
    """A fresh, namespaced ticket_id, cleaned out of tickets_seen after the
    test so the suite is re-runnable from a dirty database."""
    tid = unique_ticket_id()
    yield tid
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM tickets_seen WHERE ticket_id = %s", (tid,))
