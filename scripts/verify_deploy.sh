#!/usr/bin/env bash
# T-11 verify command: `bash scripts/verify_deploy.sh` must exit 0.
#
# What this actually proves, end to end:
#   1. The deploy/ stack (deploy/docker-compose.yml, deploy/Dockerfile.*)
#      builds and starts, and every service reports healthy.
#   2. GET /health returns 200 (through the portal's nginx proxy — the
#      same single public entry point docs/deploy.md tells a human to
#      open on the droplet).
#   3. The portal serves its built index page.
#   4. GET /api/metrics rejects a request with no X-Portal-Token as 401.
#   5. GET /api/metrics accepts a request with the correct X-Portal-Token
#      as 200.
#
# Every assertion below either passes or the script exits non-zero with a
# specific message — there is no "skip and continue" path. A precondition
# that can't be satisfied (e.g. PORTAL_TOKEN unset) is a hard failure, not
# a silently-skipped check, because a skip that still exits 0 would make
# this script lie about what it verified.
#
# Local mode (DEPLOY_HOST empty): brings the deploy stack up on this
# machine with its own docker-compose project (`othram-deploy`, pinned in
# deploy/docker-compose.yml), asserts against it, then tears it down.
# Never touches the dev stack's `othram-db` container/volume — see
# deploy/docker-compose.yml's header comment for exactly how the two stay
# isolated (separate project name, separate container names, separate
# named volume, `db` has no published host port at all).
#
# Remote mode (DEPLOY_HOST set): runs the identical HTTP assertions
# against that host over the network. Does not build, start, or stop
# anything — a remote deploy is assumed to already be running (see
# docs/deploy.md for how to put one there).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/deploy/docker-compose.yml"
CLEANUP_DONE=0

# --- source .env if present, so a human doesn't have to remember
# `set -a; source .env; set +a` themselves (matches the idiom
# .env.example already documents for doctl). ---
if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$REPO_ROOT/.env"
  set +a
  echo "[verify_deploy] sourced $REPO_ROOT/.env"
else
  echo "[verify_deploy] no .env at repo root — proceeding with whatever is already exported"
fi

PORTAL_TOKEN="${PORTAL_TOKEN:-}"
DEPLOY_HOST="${DEPLOY_HOST:-}"
PORTAL_PORT="${PORTAL_PORT:-8080}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
# Remote-only override: the port the deploy's public entry point (the
# portal container / nginx) is reachable on, if different from
# PORTAL_PORT (e.g. a droplet fronting the portal on 80/443).
DEPLOY_PORT="${DEPLOY_PORT:-$PORTAL_PORT}"
DEPLOY_SCHEME="${DEPLOY_SCHEME:-http}"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

if [ -z "$PORTAL_TOKEN" ]; then
  fail "PORTAL_TOKEN is empty. Set it in .env (repo root) before running this script — the auth assertions below cannot run without it, and a skipped assertion is not a pass."
fi

# --- HTTP assertion helpers -------------------------------------------

# assert_status METHOD URL EXPECTED_STATUS [EXTRA_CURL_ARGS...]
assert_status() {
  local method="$1" url="$2" expected="$3"
  shift 3
  local actual
  actual="$(curl -sS -o /dev/null -w '%{http_code}' -X "$method" --max-time 10 "$@" "$url" || echo "curl_error")"
  if [ "$actual" != "$expected" ]; then
    fail "$method $url expected HTTP $expected, got $actual"
  fi
  echo "  ok: $method $url -> $actual"
}

assert_body_contains() {
  local url="$1" needle="$2"
  local body
  body="$(curl -sS --max-time 10 "$url" || true)"
  case "$body" in
    *"$needle"*) echo "  ok: $url body contains '$needle'" ;;
    *) fail "$url body did not contain expected marker '$needle' (got ${#body} bytes)" ;;
  esac
}

run_assertions() {
  local base="$1"
  echo "[verify_deploy] asserting against $base"

  echo "[verify_deploy] 1/4: GET /health -> 200"
  assert_status GET "$base/health" 200

  echo "[verify_deploy] 2/4: GET / serves the portal's index"
  assert_status GET "$base/" 200
  # portal/index.html's vite react-ts template root mount point.
  assert_body_contains "$base/" 'id="root"'

  echo "[verify_deploy] 3/4: GET /api/metrics with no token -> 401"
  assert_status GET "$base/api/metrics" 401

  echo "[verify_deploy] 4/4: GET /api/metrics with X-Portal-Token -> 200"
  assert_status GET "$base/api/metrics" 200 -H "X-Portal-Token: $PORTAL_TOKEN"
}

# --- local mode ----------------------------------------------------------

verify_local() {
  echo "[verify_deploy] DEPLOY_HOST is empty -> verifying the LOCAL deploy stack (deploy/docker-compose.yml)."
  echo "[verify_deploy] this does NOT verify a remote/droplet deployment — see docs/deploy.md to stand one up and rerun with DEPLOY_HOST set."

  command -v docker >/dev/null 2>&1 || fail "docker is not installed/on PATH"
  docker info >/dev/null 2>&1 || fail "docker daemon is not running"

  # A *global* flag, not `local`: the EXIT trap fires after this function
  # has already returned (the script's last line is below, outside
  # verify_local), by which point any `local` variable from this function
  # is out of scope — under `set -u` that made the trap itself blow up
  # with "unbound variable" and skip `docker compose down` entirely,
  # leaving the stack running. Caught by actually running this script
  # rather than just reading it.
  cleanup() {
    if [ "$CLEANUP_DONE" -eq 0 ]; then
      CLEANUP_DONE=1
      echo "[verify_deploy] tearing down the local deploy stack (othram-deploy project) ..."
      docker compose -f "$COMPOSE_FILE" down -v --remove-orphans >&2 || true
    fi
  }
  trap cleanup EXIT

  echo "[verify_deploy] building and starting deploy/docker-compose.yml (project: othram-deploy) ..."
  BACKEND_PORT="$BACKEND_PORT" PORTAL_PORT="$PORTAL_PORT" \
    docker compose -f "$COMPOSE_FILE" up -d --build --wait --wait-timeout 300 \
    || fail "docker compose up --wait did not reach a healthy state within the timeout; see 'docker compose -f $COMPOSE_FILE logs' for details"

  echo "[verify_deploy] every service reports healthy."

  run_assertions "http://127.0.0.1:${PORTAL_PORT}"

  echo "[verify_deploy] all assertions passed against the LOCAL stack."
  echo "[verify_deploy] NOTE: this verified the stack running on THIS machine, not a deployed droplet. DEPLOY_HOST is empty in .env — remote deployment has not been verified."
}

# --- remote mode -----------------------------------------------------------

verify_remote() {
  echo "[verify_deploy] DEPLOY_HOST=$DEPLOY_HOST -> verifying the REMOTE deploy over the network. No local build/start/stop will happen."

  run_assertions "${DEPLOY_SCHEME}://${DEPLOY_HOST}:${DEPLOY_PORT}"

  echo "[verify_deploy] all assertions passed against REMOTE host $DEPLOY_HOST."
}

# --- entrypoint --------------------------------------------------------

if [ -z "$DEPLOY_HOST" ]; then
  verify_local
else
  verify_remote
fi

echo "[verify_deploy] PASS"
