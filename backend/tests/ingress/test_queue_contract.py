"""The frozen queue contract (DESIGN § *Frozen interface contracts* §1.1,
ADR-002), tested without Redis, Postgres or a network.

Track F's compose services are written against three strings that live
nowhere else: the queue name `cxforge:jobs`, the arq task name `run_ticket`,
and the worker command `arq worker.main.WorkerSettings`. Nothing else in
the suite would notice if one of them drifted — a producer publishing to
`arq:queue` and a consumer polling `cxforge:jobs` both start cleanly, log
nothing, and simply never exchange a job. These tests pin all three.

Deliberately in `backend/tests/ingress/` rather than a new
`backend/tests/worker/`: `backend/tests/plan/test_blast_radius.py` maps a
new test directory to a graph node and turns 4–11 closed tickets red
(`docs/BUILD-PLAN.md §3`, blast-radius trap). `ingress` and `portal` are the
only two suite directories whose closure already contains `data` + `main`.

No `SKIP_DB_TESTS` guard here on purpose: this module touches no database,
so it is one of the few parts of the dispatch wiring CI can actually run.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from worker import main as worker_main
from worker import queue as worker_queue
from worker.jobs import TicketJob
from worker.settings import DEFAULT_REDIS_URL, QUEUE_NAME, RUN_TICKET_TASK, redis_url

# The exact strings docs/DESIGN.md §1.1 freezes. Written as literals, not
# imported constants, so a rename in worker/settings.py fails here instead
# of silently agreeing with itself.
FROZEN_QUEUE_NAME = "cxforge:jobs"
FROZEN_TASK_NAME = "run_ticket"


class _StubPool:
    """Stands in for `arq.connections.ArqRedis` — records the publish call
    verbatim instead of talking to Redis."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.closed = False

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((function, args, kwargs))

    async def aclose(self) -> None:
        self.closed = True


def test_frozen_names_match_design_1_1() -> None:
    assert QUEUE_NAME == FROZEN_QUEUE_NAME
    assert RUN_TICKET_TASK == FROZEN_TASK_NAME


def test_worker_settings_is_the_arq_entrypoint_track_f_runs() -> None:
    """`arq worker.main.WorkerSettings` is the container command Track F is
    building compose services against."""
    settings = worker_main.WorkerSettings

    assert settings.queue_name == FROZEN_QUEUE_NAME
    registered = {getattr(f, "name", None) for f in settings.functions}
    assert FROZEN_TASK_NAME in registered, (
        f"arq would not answer to '{FROZEN_TASK_NAME}'; registered: {registered}"
    )
    # An arq retry re-runs the agent and posts a second public reply. The
    # `tickets_seen` row does NOT prevent that — `worker.main` never reads it.
    # `max_tries=1` is the only guard.
    from arq.worker import Function

    (function,) = [f for f in settings.functions if getattr(f, "name", None) == FROZEN_TASK_NAME]
    assert isinstance(function, Function)
    assert function.max_tries == 1


def test_worker_settings_reach_a_real_arq_worker_with_a_survivable_timeout() -> None:
    """Assert the *actuals* arq computes, not the class body.

    arq's default `job_timeout` is 300s. A run makes 3+ Anthropic calls and
    the SDK's default read timeout is 600s with 2 retries, so the default
    cancels healthy runs — and a cancellation is an `asyncio.CancelledError`,
    which is a `BaseException` that `except Exception` does not catch.
    """
    from arq.worker import create_worker

    # arq reads settings out of `settings_cls.__dict__` only. It will not call
    # a callable or resolve a descriptor — a `property` or `staticmethod` is
    # passed through *unresolved* and `Worker` accepts it silently, failing
    # later at connect with a confusing error. So the value must be a concrete
    # object literally in the class body.
    assert "job_timeout" in worker_main.WorkerSettings.__dict__, (
        "job_timeout is not in the class __dict__, so arq falls back to its "
        "300s default and cancels healthy runs"
    )

    async def _build() -> Any:
        # Constructed inside a running loop because that is what
        # `arq.run_worker` does; outside one, arq's own
        # `asyncio.get_event_loop()` emits a DeprecationWarning.
        #
        # `handle_signals=False` is test-only: it stops the Worker installing
        # SIGTERM handlers that pytest later reports as an unraisable
        # exception. A deployed worker keeps the default and shuts down cleanly.
        return create_worker(worker_main.WorkerSettings, handle_signals=False)

    worker = asyncio.run(_build())

    assert worker.job_timeout_s >= 900, worker.job_timeout_s
    assert worker.queue_name == FROZEN_QUEUE_NAME


def test_get_job_queue_returns_the_real_arq_queue() -> None:
    """The production dependency, asserted directly.

    Every ingress test overrides `get_job_queue`, and `test_arq_job_queue_*`
    instantiates `ArqJobQueue` by hand — so if this function started returning
    a no-op tomorrow, the entire suite would stay green while no webhook ever
    reached Redis again. That is a fresh instance of the exact defect W1-A
    exists to fix.
    """
    assert isinstance(worker_queue.get_job_queue(), worker_queue.ArqJobQueue)


def test_redis_url_reads_the_environment_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this app calls `load_dotenv()` (docs/STATE.md §6.14), so an
    import-time read would freeze whatever was set when the module first
    loaded — including nothing at all."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert redis_url() == DEFAULT_REDIS_URL

    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/3")
    assert redis_url() == "redis://redis:6379/3"


def test_arq_job_queue_publishes_run_ticket_onto_cxforge_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The producer half of the contract: task name, queue name (on both the
    pool and the publish call), a JSON-round-trippable payload, and the
    connection actually closed."""
    pool = _StubPool()
    seen: dict[str, Any] = {}

    async def _fake_create_pool(settings: Any, **kwargs: Any) -> _StubPool:
        seen["default_queue_name"] = kwargs.get("default_queue_name")
        return pool

    monkeypatch.setattr(worker_queue, "create_pool", _fake_create_pool)

    job = TicketJob(
        ticket_id="ticket-42",
        comment_id="comment-7",
        received_at=datetime(2026, 8, 16, 12, 0, 0, 123456, tzinfo=UTC),
    )
    asyncio.run(worker_queue.ArqJobQueue().enqueue(job))

    assert seen["default_queue_name"] == FROZEN_QUEUE_NAME
    assert len(pool.calls) == 1
    function, args, kwargs = pool.calls[0]
    assert function == FROZEN_TASK_NAME
    assert kwargs["_queue_name"] == FROZEN_QUEUE_NAME
    # No _job_id: arq would use it to deduplicate and would swallow the
    # re-fired Zendesk trigger that ADR-003 relies on for recovery.
    assert kwargs.get("_job_id") is None
    assert pool.closed is True

    # The payload the consumer will rebuild — including received_at, which
    # must survive JSON rather than depending on arq's pickle serializer.
    (payload,) = args
    assert TicketJob.model_validate(payload) == job
    assert payload["received_at"] == "2026-08-16T12:00:00.123456Z"


# --------------------------------------------------------------------------
# W1-F4's other half: the worker loads .env itself, because
# `arq worker.main.WorkerSettings` never imports `backend/src/main.py`.
# --------------------------------------------------------------------------


def test_worker_repo_root_is_the_repo_root_not_the_backend_dir() -> None:
    """`backend/src/worker/main.py` is one directory deeper than
    `backend/src/main.py`, so it needs `parents[3]` where that file needs
    `parents[2]`. Off by one and `load_dotenv` silently points at
    `backend/.env`, which does not exist — and a missing `.env` is not an
    error, so the worker would start with no credentials and no complaint.

    The expected value is derived independently, from this test file's own
    location (`backend/tests/ingress/` is also three deep).
    """
    expected = Path(__file__).resolve().parents[3]

    assert worker_main.REPO_ROOT == expected
    assert (worker_main.REPO_ROOT / "pyproject.toml").is_file()
    assert worker_main.REPO_ROOT.name != "backend"


def test_worker_dotenv_load_is_gated_off_inside_pytest() -> None:
    """Matches `backend/src/main.py` and `backend/src/data/db.py`: a test
    process must not inherit the developer's live Zendesk trial and real
    `ANTHROPIC_API_KEY` just because it imported the worker."""
    assert worker_main._running_under_pytest() is True


def test_worker_actually_loads_dotenv_outside_pytest(tmp_path: Path) -> None:
    """The gate above makes the load unobservable in-process, so prove it in a
    child interpreter — otherwise "the worker reads `.env`" is a claim about
    code shape, not about behaviour.

    The child is launched with `PYTEST_VERSION` popped (so the gate opens) AND
    with the probe variable popped from its inherited environment (so a parent
    shell that ran `set -a; source .env; set +a` cannot make this pass on its
    own). It prints a boolean, never a value.
    """
    repo_root = Path(__file__).resolve().parents[3]
    env_file = repo_root / ".env"
    if not env_file.is_file():
        pytest.skip("no .env in this checkout (CI); nothing for load_dotenv to read")

    probe_key = next(
        (
            line.split("=", 1)[0].strip()
            for line in env_file.read_text().splitlines()
            if "=" in line and not line.lstrip().startswith("#") and line.split("=", 1)[1].strip()
        ),
        None,
    )
    assert probe_key, "could not find a populated key in .env to probe with"

    child_env = {k: v for k, v in os.environ.items() if k not in {"PYTEST_VERSION", probe_key}}
    child_env["PYTHONPATH"] = str(repo_root / "backend" / "src")

    script = (
        "import os, worker.main as m;"
        f"print('LOADED', os.environ.get({probe_key!r}) is not None);"
        "print('ROOT', m.REPO_ROOT)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=child_env,
        capture_output=True,
        text=True,
        cwd=tmp_path,  # not the repo root: the path must come from __file__
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "LOADED True" in result.stdout, (
        f"importing worker.main outside pytest did not populate {probe_key} — "
        f"the worker process would start with no credentials.\n{result.stdout}\n{result.stderr}"
    )
    assert f"ROOT {repo_root}" in result.stdout


def test_worker_settings_snapshot_reflects_the_process_environment(tmp_path: Path) -> None:
    """`WorkerSettings.redis_settings` is the one import-time read of
    `REDIS_URL` (arq requires a concrete value in the class `__dict__`), so
    prove the snapshot actually sees the environment the worker starts with —
    which in the container is compose's `REDIS_URL: redis://redis:6379/0`.

    A child interpreter is the only honest way to test an import-time read.
    """
    repo_root = Path(__file__).resolve().parents[3]
    child_env = {k: v for k, v in os.environ.items() if k != "PYTEST_VERSION"}
    child_env["PYTHONPATH"] = str(repo_root / "backend" / "src")
    child_env["REDIS_URL"] = "redis://probe-host:6399/7"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import worker.main as m;"
            "s = m.WorkerSettings.redis_settings;"
            "print('HOST', s.host); print('PORT', s.port); print('DB', s.database)",
        ],
        env=child_env,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "HOST probe-host" in result.stdout, (
        "the arq settings snapshot ignored REDIS_URL — a worker container "
        f"would connect to the wrong broker.\n{result.stdout}\n{result.stderr}"
    )
    assert "PORT 6399" in result.stdout
    assert "DB 7" in result.stdout


def test_ticket_job_requires_received_at() -> None:
    """DESIGN §1.1 pins three fields. A job without a receipt stamp would
    silently reintroduce the `datetime.now(UTC)` defect ADR-004 fixes."""
    with pytest.raises(ValidationError):
        TicketJob.model_validate({"ticket_id": "t", "comment_id": "c"})
