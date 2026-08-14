#!/bin/sh
# Container entrypoint for the backend image (deploy/Dockerfile.backend).
#
# Runs the deploy-time DB bootstrap (schema creation + optional fixture
# seed — see bootstrap.py in this directory) before handing off to the
# real command (uvicorn, from the Dockerfile's CMD / docker-compose's
# `command`, passed through here as "$@"). This is the one thing a bare
# `uvicorn main:app` couldn't do on its own: `backend/src/data/schema.py`'s
# `init_schema` has never been wired to any app-startup hook (T-1..T-10
# call it explicitly from tests/seeders, never from `main.py`, and
# `main.py` is out of T-11's edit scope) — so the production image has to
# call it itself, once, before the app starts accepting traffic.
set -eu

echo "[entrypoint] running deploy bootstrap (schema + seed)..." >&2
python /app/deploy_bootstrap.py

echo "[entrypoint] bootstrap complete, starting: $*" >&2
exec "$@"
