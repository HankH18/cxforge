"""T-0 acceptance: the harness every downstream ticket's verify command needs.

Deliberately no business logic — these tests assert that the skeleton, the
marker registry and the docker-compose database are real.
"""

import importlib
import os
import time

import psycopg
import pytest
from fastapi.testclient import TestClient

from main import app

DEFAULT_DSN = "postgresql://othram:othram@localhost:5432/othram"

# Component packages T-1..T-8 fill in. Importable from the start so no later
# ticket has to create a package outside its own file scope.
COMPONENT_PACKAGES = [
    "data",
    "helpdesk",
    "agent",
    "escalation",
    "ingress",
    "portal",
    "evals",
]


def test_health_endpoint_serves() -> None:
    """The FastAPI stub app boots and answers its liveness probe."""
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("package", COMPONENT_PACKAGES)
def test_component_package_importable(package: str) -> None:
    """Every component package exists so its owning ticket stays in scope.

    Asserts a real ``__init__.py``, not merely an importable path: Python 3.12
    resolves a bare directory as a PEP 420 namespace package, so an import
    alone would still succeed after the file was deleted.
    """
    module = importlib.import_module(package)
    assert module.__file__ is not None, (
        f"{package} resolved as a namespace package — its __init__.py is missing"
    )
    assert module.__file__.endswith("__init__.py")


@pytest.mark.parametrize("marker", ["contract", "grounding", "live"])
def test_marker_registered(request: pytest.FixtureRequest, marker: str) -> None:
    """--strict-markers means an unregistered marker is a collection error."""
    registered = {line.split(":", 1)[0] for line in request.config.getini("markers")}
    assert marker in registered


def test_collection_is_confined_to_backend_tests(request: pytest.FixtureRequest) -> None:
    """testpaths must keep collection out of portal/ and node_modules/."""
    assert request.config.getini("testpaths") == ["backend/tests"]


@pytest.mark.skipif(
    os.getenv("SKIP_DB_TESTS") == "1",
    reason="requires the docker-compose db service (CI sets SKIP_DB_TESTS=1)",
)
def test_database_is_postgres_16_with_pgvector() -> None:
    """`docker compose up -d db` yields a healthy Postgres 16 carrying pgvector.

    The verify chain starts the container immediately before pytest runs, so
    poll briefly rather than racing the healthcheck.
    """
    dsn = os.getenv("DATABASE_URL", DEFAULT_DSN)
    deadline = time.monotonic() + 60
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT current_setting('server_version')")
                    version_row = cur.fetchone()
                    assert version_row is not None
                    assert version_row[0].startswith("16."), (
                        f"expected Postgres 16, got {version_row[0]}"
                    )

                    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    conn.commit()
                    cur.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
                    assert cur.fetchone() is not None, "pgvector extension unavailable"
            return
        except psycopg.OperationalError as exc:  # container still starting
            last_error = exc
            time.sleep(1)
    pytest.fail(f"database never became reachable at {dsn}: {last_error}")
