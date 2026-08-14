"""Shared fixtures for the portal API test suite (DESIGN §Portal API /
§Metric definitions, SPEC R10-R13).

Every test here needs the docker-compose Postgres (``runs``/``drafts``/
``settings``), so the whole suite is skipped when ``SKIP_DB_TESTS=1`` (CI
has no db service) via the root ``backend/tests/conftest.py``'s
``pytest_collection_modifyitems`` hook — a conftest-level ``pytestmark``
here would not apply to the sibling test modules in this directory (T-16),
so the skip is applied at collection time instead. Every test posts
through the real FastAPI app (``main.app``) via ``TestClient`` and the
real ``X-Portal-Token`` auth dependency — ``PORTAL_TOKEN`` is
monkeypatched to a fixed value so tests never depend on whatever (if
anything) the host's real ``.env`` has.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from data import get_connection, init_schema
from main import app
from portal.deps import get_helpdesk_port

from ._fake_port import FakeHelpdeskPort

TEST_PORTAL_TOKEN = "portal-test-token-do-not-use-in-prod"
AUTH_HEADERS = {"X-Portal-Token": TEST_PORTAL_TOKEN}


@pytest.fixture(autouse=True)
def _portal_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``PORTAL_TOKEN`` for every test, overriding (and restoring, via
    monkeypatch) whatever the host environment has — mirrors
    ``backend/tests/ingress/conftest.py``'s ``ZENDESK_WEBHOOK_SIGNING_SECRET``
    handling."""
    monkeypatch.setenv("PORTAL_TOKEN", TEST_PORTAL_TOKEN)


@pytest.fixture(scope="session")
def _schema_ready() -> None:
    """``runs``/``drafts``/``settings`` already exist (T-1's
    ``init_schema``), but this suite must not assume it ran after T-1's own
    tests against a fresh database — ``init_schema`` is idempotent (``CREATE
    TABLE IF NOT EXISTS``), so calling it here is always safe."""
    with get_connection() as conn:
        init_schema(conn)


@pytest.fixture(autouse=True)
def _clean_run_tables(_schema_ready: None) -> None:
    """Truncate before each test so one test's gate setting or recorded
    runs/drafts never leak into the next — mirrors
    ``backend/tests/graph/conftest.py``'s ``_clean_run_tables``. The suite
    is re-runnable from a dirty database because every test starts from a
    truncated, known-empty state rather than depending on cleanup that ran
    (or didn't) after some earlier failed run."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE drafts, runs, settings RESTART IDENTITY")


@pytest.fixture
def client(_schema_ready: None) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def fake_port() -> Iterator[FakeHelpdeskPort]:
    """Overrides the app's real ``get_helpdesk_port`` dependency
    (``portal.deps`` — normally a live ``ZendeskAdapter``) with an
    in-memory recorder for the duration of one test, via FastAPI's own
    ``dependency_overrides`` seam — never a monkeypatched module global."""
    port = FakeHelpdeskPort()
    app.dependency_overrides[get_helpdesk_port] = lambda: port
    try:
        yield port
    finally:
        app.dependency_overrides.pop(get_helpdesk_port, None)
