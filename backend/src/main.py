"""FastAPI application root.

Mounts one router per component package. The routers are declared empty in
T-0 and filled in by the ticket that owns each package (ingress → T-4,
portal → T-8).

W1-F4 added the `.env` load below. `docs/STATE.md §6.14`: nothing in the app
or the scripts called ``load_dotenv()``, so every documented run command saw a
completely empty credential set even though `.env` was fully populated —
``scripts/live_smoke.py`` printed "credentials absent" and exited 0, and the
webhook 500'd on a missing signing secret. The deploy path only worked
because `docs/deploy.md:151` tells the operator to type
``set -a; source .env; set +a`` first.

("In the app or the scripts" is the precise claim, and it is STATE's. One
caller did already exist — ``evals/report.py:411`` — outside both.)
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

# backend/src/main.py -> backend/src -> backend -> repo root. Derived from
# this file's own location rather than from the working directory, so
# `uvicorn main:app` finds the same `.env` whatever directory it is launched
# from.
REPO_ROOT = Path(__file__).resolve().parents[2]


def load_repo_dotenv(repo_root: Path = REPO_ROOT) -> bool:
    """Load ``<repo_root>/.env`` into ``os.environ``. Returns whether it existed.

    ``override=False`` is the important half: **anything already exported in
    the real environment wins.** That is what keeps this safe in a container,
    where every credential arrives through `docker compose`'s ``environment:``
    block and a stray `.env` inside the image (there is none, deliberately)
    must never be able to shadow it. It is also the same precedence rule
    `.env.example` already states for ``DEPLOY_HOST``: sourcing a file cannot
    clobber a variable the operator exported on purpose.

    A missing `.env` is not an error — production has none.
    """
    env_path = repo_root / ".env"
    if not env_path.is_file():
        return False
    load_dotenv(env_path, override=False)
    return True


def _running_under_pytest() -> bool:
    """Same structural gate `backend/src/data/db.py` uses, for the same reason.

    A test process must not silently inherit the developer's real credentials.
    This repo has a live Zendesk trial and a real ``ANTHROPIC_API_KEY`` sitting
    in `.env`; importing this module during a test run would put both into
    ``os.environ`` for every test in the session, so a test that forgot to
    stub something would reach a real API instead of failing. It would also
    make local runs and CI (which has no `.env`) disagree about what the
    environment contains — the divergence is the bug, whichever way it goes.

    The load itself is covered directly by
    ``backend/tests/deploy/test_dotenv_loading.py``, including a subprocess
    that proves the module-level call below actually fires outside pytest.
    """
    return "PYTEST_VERSION" in os.environ


if not _running_under_pytest():  # pragma: no cover - proven by subprocess test
    load_repo_dotenv()

# Imported after the load so that any module resolving configuration at import
# time sees a populated environment.
from ingress import router as ingress_router  # noqa: E402
from portal import router as portal_router  # noqa: E402

app = FastAPI(title="Othram AI Support Agent", version="0.1.0")

app.include_router(ingress_router)
app.include_router(portal_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe, used by the deploy verification script (T-11)."""
    return {"status": "ok"}
