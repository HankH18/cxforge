"""Deploy-time DB bootstrap for the production backend image (T-11).

This file lives under ``deploy/`` — W1-F's scope — not ``backend/src/**``.
It does not modify any T-1..T-10 code; it only *calls* functions those
tickets already committed and own (``data.schema.init_schema``,
``data.seed.seed_all``), the same way ``backend/tests/data/conftest.py``
or any other caller does. Nothing here duplicates or reimplements schema
or seeding logic.

Why this needs to exist at all: ``backend/src/main.py`` never calls
``init_schema`` itself — every other caller is a test fixture or the
standalone seeder script, run against the dev Postgres by hand. A freshly
created production Postgres has no tables at all, so without this, the very
first request to ``GET /api/metrics`` would fail with a "relation runs does
not exist" 500 instead of the schema-correct empty-state response
``portal.service.compute_metrics`` returns for a DB with tables but no rows.
Running this once before ``uvicorn`` starts (see ``entrypoint.sh``) closes
that gap without touching ``backend/**``.

SEEDING IS NOW CONDITIONAL, AND THAT IS A DELIBERATE CHANGE (W1-F, 2026-08-16)
=============================================================================
``data.seed.seed_all`` starts by TRUNCATE-ing ``cases`` and ``kb_chunks``
(``seed.py:57`` and ``:105``). This entrypoint runs on **every** container
start, and the compose services carry ``restart: unless-stopped`` — and a
compose ``depends_on`` condition governs ``up`` ordering only, **not**
restarts and not what the daemon does after a reboot.

So as originally written, any backend crash-and-restart re-truncated and
reloaded the knowledge base **while the worker was already running and
consuming jobs**. A retrieval in flight would see an empty ``kb_chunks``:
an ungrounded reply or a hard failure, with nothing in any log connecting it
to the restart. Moving the worker off this entrypoint removed the second
seeder; it did not remove that hazard, because the hazard was never about
two seeders.

``SEED_ON_START`` therefore now selects between three modes:

- **unset / ``true`` / anything unrecognised → seed only if the KB is empty.**
  A fresh deploy is seeded and immediately demo-able, exactly as before; a
  restart of a populated database leaves it alone and says so. This is the
  default because it is the only one that is safe to run repeatedly.
- **``false`` / ``0`` / ``no`` / ``off`` → create the schema and stop.**
  Unchanged in meaning, so `docs/deploy.md`'s existing advice to set it on an
  already-live deploy stays correct — merely no longer necessary.
- **``force`` → always seed, truncating whatever is there.** The old
  unconditional behaviour, kept as a deliberate one-word opt-in for when you
  really do want the fixtures reloaded.

Seeding uses ``HashingEmbedder`` (``backend/src/data/embeddings.py``) — a
fully offline, deterministic lexical embedder — so it needs no model
credentials at all: it runs identically whether or not ``ANTHROPIC_API_KEY``
is set. ``seed_all`` never touches ``runs``/``drafts``/``settings`` in any
mode.
"""

from __future__ import annotations

import os
import sys

from data.db import get_connection
from data.schema import init_schema

_FALSY = {"false", "0", "no", "off"}
_FORCE = {"force", "reseed"}

MODE_NEVER = "never"
MODE_IF_EMPTY = "if-empty"
MODE_FORCE = "force"


def seed_mode(raw: str | None) -> str:
    """Map ``SEED_ON_START`` to one of the three modes above.

    Unrecognised values fall to ``if-empty`` rather than to ``force``: a typo
    in an env var must not be able to truncate a populated knowledge base.
    """
    value = (raw or "").strip().lower()
    if value in _FALSY:
        return MODE_NEVER
    if value in _FORCE:
        return MODE_FORCE
    return MODE_IF_EMPTY


def should_seed(mode: str, *, case_count: int, kb_chunk_count: int) -> bool:
    """Whether to run the destructive ``seed_all`` for this mode and DB state."""
    if mode == MODE_NEVER:
        return False
    if mode == MODE_FORCE:
        return True
    return case_count == 0 and kb_chunk_count == 0


def _content_counts() -> tuple[int, int]:
    """``(cases, kb_chunks)`` row counts, after ensuring the schema exists."""
    with get_connection() as conn:
        init_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT (SELECT count(*) FROM cases), (SELECT count(*) FROM kb_chunks)")
            row = cur.fetchone()
    if row is None:  # pragma: no cover - a scalar subquery always returns a row
        return (0, 0)
    return (int(row[0]), int(row[1]))


def main() -> None:
    mode = seed_mode(os.environ.get("SEED_ON_START"))

    if mode == MODE_NEVER:
        with get_connection() as conn:
            init_schema(conn)
        print("[bootstrap] schema ready (SEED_ON_START=false, no seed)", file=sys.stderr)
        return

    case_count, kb_chunk_count = _content_counts()

    if not should_seed(mode, case_count=case_count, kb_chunk_count=kb_chunk_count):
        print(
            f"[bootstrap] schema ready; NOT seeding — {case_count} cases and "
            f"{kb_chunk_count} kb chunks are already present. seed_all() "
            f"TRUNCATEs both tables, and the worker may be mid-run against "
            f"them. Set SEED_ON_START=force to reload the fixtures anyway.",
            file=sys.stderr,
        )
        return

    from data.seed import seed_all

    result = seed_all()
    print(
        f"[bootstrap] schema ready; seeded {result.case_count} cases, "
        f"{result.kb_chunk_count} kb chunks (mode={mode})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
