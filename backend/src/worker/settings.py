"""Redis connection + queue naming for the cxforge job queue (ADR-002).

The queue name and the arq task name are **frozen contract**
(`docs/DESIGN.md` §1.1): Track F's compose services are written against
`cxforge:jobs` and against the worker command `arq worker.main.WorkerSettings`.
They live here as module constants so ingress (the producer) and
`worker.main` (the consumer) can never drift apart by a typo — a string
literal repeated in two files is exactly how a queue silently splits in two.

`redis_url()` and `redis_settings()` read `REDIS_URL` at **call time**, not at
import time, so nothing here freezes an environment that a later
`load_dotenv()` or `set -a; source .env` would have populated.

**One documented exception:** `worker.main.WorkerSettings.redis_settings` is a
snapshot taken once, in that class body. arq reads its settings out of
`settings_cls.__dict__` (`arq.worker.get_kwargs`) and will not call a callable
or resolve a descriptor, so a concrete `RedisSettings` instance is the only
thing that works there. `worker/main.py` loads `.env` above that class body
specifically so the snapshot is taken from a populated environment; the
ordering is pinned by a test.
"""

from __future__ import annotations

import os

from arq.connections import RedisSettings

# arq's own default is "arq:queue"; ours is namespaced so a shared Redis
# never mixes cxforge jobs with anything else.
QUEUE_NAME = "cxforge:jobs"

# The arq task name the worker registers and ingress enqueues against.
RUN_TICKET_TASK = "run_ticket"

# Matches the `redis` service name in both compose files (Track F, W1-F1).
DEFAULT_REDIS_URL = "redis://localhost:6379"


def redis_url() -> str:
    """The broker DSN. `REDIS_URL` unset (or empty) falls back to a local
    Redis, which is what the non-live test suite and a bare `uvicorn` on a
    laptop see."""
    return os.environ.get("REDIS_URL") or DEFAULT_REDIS_URL


def redis_settings() -> RedisSettings:
    """arq's connection settings, parsed from `redis_url()`.

    Constructing these opens no socket — a bad DSN fails here, a dead Redis
    fails at `create_pool`. That distinction is what lets `worker.main` be
    imported (and its `WorkerSettings` introspected) with no Redis running.
    """
    return RedisSettings.from_dsn(redis_url())
