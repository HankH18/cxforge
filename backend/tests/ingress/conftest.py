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

import base64
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from data import get_connection, init_schema
from main import app

# A fixed, valid-base64 signing secret used by every test in this suite —
# independent of whatever (if anything) is in the host's real .env, so this
# suite never depends on real Zendesk credentials existing.
TEST_SIGNING_SECRET = base64.b64encode(b"ingress-test-signing-secret").decode("utf-8")
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


@pytest.fixture
def client(_schema_ready: None) -> Iterator[TestClient]:
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
