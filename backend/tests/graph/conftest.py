"""Shared fixtures for the graph test suite — T-5's in-process, end-to-end
coverage of the pinned pipeline (``uv run pytest backend/tests/graph -q``).

Real Postgres (via ``data.seed.seed_all``/``data.get_case``/``data.search_kb``),
a fake ``LLMClient`` (``fakes.FakeLLMClient``), and a fake ``HelpdeskPort``
(``helpdesk.email_adapter.EmailAdapter`` reused as an in-memory recorder) —
the two external services (OpenAI, Zendesk) are faked; the data layer this
ticket doesn't own is exercised for real, exactly as
``backend/tests/data``'s own suite does.

Skips itself when ``SKIP_DB_TESTS=1`` (CI has no db service), mirroring
``backend/tests/data/conftest.py``'s convention.
"""

from __future__ import annotations

import os

import pytest

from agent.config import GATE_SETTING_KEY
from data import get_connection
from data.seed import SeedResult, seed_all
from helpdesk.email_adapter import EmailAdapter

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_DB_TESTS") == "1",
    reason="requires the docker-compose db service (CI sets SKIP_DB_TESTS=1)",
)


@pytest.fixture(scope="session")
def seeded() -> SeedResult:
    """Seed cases + KB once per session — ``seed_all`` truncates-and-reloads
    every call, so the suite is re-runnable from a dirty database."""
    return seed_all()


@pytest.fixture(autouse=True)
def _clean_run_tables(seeded: SeedResult) -> None:
    """Every graph run reads the gate and writes ``runs``/``drafts`` —
    truncate before each test so one test's gate setting or recorded runs
    never leak into the next."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE drafts, runs, settings RESTART IDENTITY")


def set_gate(enabled: bool) -> None:
    """Test helper: write R11's gate directly (T-8 owns the real write
    path, ``PUT /api/settings/gate`` — not built yet), via the same
    ``settings`` table ``agent.store.read_gate_enabled`` reads."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (GATE_SETTING_KEY, "true" if enabled else "false"),
        )


@pytest.fixture
def port() -> EmailAdapter:
    return EmailAdapter()


def seed_conversation(port: EmailAdapter, *, requester_email: str, message: str) -> str:
    """Seed a fresh ticket with one public customer message — the minimal
    shape ``ingest`` expects to rebuild conversation context from (R7)."""
    ticket_id = port.seed_ticket(requester_email=requester_email)
    port.seed_comment(ticket_id, author="customer", text=message)
    return ticket_id
