"""Deploy-time DB bootstrap for the production backend image (T-11).

This file lives under ``deploy/`` — T-11's scope — not ``backend/src/**``.
It does not modify any T-1..T-10 code; it only *calls* functions those
tickets already committed and own (``data.schema.init_schema``,
``data.seed.seed_all``), the same way ``backend/tests/data/conftest.py``
or any other caller does. Nothing here duplicates or reimplements schema
or seeding logic.

Why this needs to exist at all: ``backend/src/main.py`` (T-0, out of
scope) never calls ``init_schema`` itself — every existing caller is a
test fixture or the standalone seeder script, run against the dev
Postgres by hand. A freshly created production Postgres has no tables at
all, so without this, the very first request to ``GET /api/metrics``
would fail with an "relation runs does not exist" 500 instead of the
schema-correct empty-state response ``portal.service.compute_metrics``
returns for a DB with tables but no rows. Running this once before
``uvicorn`` starts (see ``entrypoint.sh``) closes that gap without
touching ``backend/**``.

Controlled by the ``SEED_ON_START`` env var (default ``true``):

- ``true`` (default): create the schema if missing, then truncate-and-
  reload ``cases``/``kb_chunks`` from ``fixtures/`` (``data.seed.seed_all``
  is idempotent and safe to rerun — see that module's docstring). This is
  what makes a fresh deploy immediately demo-able: the portal feed and KB
  grounding have real fixture content on first boot, not an empty DB.
  Seeding uses ``HashingEmbedder`` (``backend/src/data/embeddings.py``) —
  a fully offline, deterministic lexical embedder — so seeding needs no
  model credentials at all: it runs identically whether or not
  ``ANTHROPIC_API_KEY`` is set.
- ``false``: create the schema if missing and stop there. Use this on a
  restart of an already-seeded, already-in-use deploy (real ``runs``/
  ``drafts`` rows exist) where re-seeding ``cases``/``kb_chunks`` on every
  container restart is undesired churn — ``seed_all`` never touches
  ``runs``/``drafts``/``settings``, so this is about avoiding unnecessary
  work, not data loss.
"""

from __future__ import annotations

import os
import sys

from data.db import get_connection
from data.schema import init_schema

_FALSY = {"false", "0", "no", "off"}


def _seed_on_start() -> bool:
    return os.environ.get("SEED_ON_START", "true").strip().lower() not in _FALSY


def main() -> None:
    if _seed_on_start():
        from data.seed import seed_all

        result = seed_all()
        print(
            f"[bootstrap] schema ready; seeded {result.case_count} cases, "
            f"{result.kb_chunk_count} kb chunks",
            file=sys.stderr,
        )
    else:
        with get_connection() as conn:
            init_schema(conn)
        print("[bootstrap] schema ready (SEED_ON_START=false, no seed)", file=sys.stderr)


if __name__ == "__main__":
    main()
