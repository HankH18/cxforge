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
# READ THIS BEFORE TRUSTING A PASS FROM THE FIVE ASSERTIONS ABOVE.
# Every one of them is a LIVENESS check. Not one makes a model call, writes
# a row, or touches the agent path — which is exactly why this script
# reported 4/4 for weeks against a stack with no ANTHROPIC_API_KEY at all,
# and still reports 4/4 against a droplet whose webhook accepts events and
# never starts a run (docs/STATE.md §6.2). A check that cannot fail when
# the product is broken is worse than no check: it manufactures confidence.
# The bare PASS line at the bottom of this script says so out loud, and
# says it every time, rather than leaving the caller to remember it.
#
# --deep (W3-G2) is the check that CAN fail when the product is broken. It
# POSTs a correctly HMAC-signed synthetic webhook at the real endpoint and
# then waits for the effect — a NEW `runs` row, read back through the
# deployed portal API — so it exercises ingress -> Redis -> the arq worker
# -> run_agent for real. See scripts/verify_core_loop.py's module docstring
# for what it asserts, how it rules out a stale row, and what it needs.
#
# Every assertion below either passes or the script exits non-zero with a
# specific message — there is no "skip and continue" path. A precondition
# that can't be satisfied (e.g. PORTAL_TOKEN unset, or --deep with no
# signing secret) is a hard failure, not a silently-skipped check, because
# a skip that still exits 0 would make this script lie about what it
# verified.
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
#
# Usage:
#   bash scripts/verify_deploy.sh                 # REMOTE (DEPLOY_HOST), liveness only
#   bash scripts/verify_deploy.sh --local         # LOCAL docker-compose stack
#   CXFORGE_VERIFY_TICKET_ID=<id> \
#     bash scripts/verify_deploy.sh --deep        # liveness + the core loop
#
# --deep is NOT read-only: it POSTs a real webhook, which makes the
# deployment run a real agent turn (model tokens spent, a real reply
# posted on that ticket) and write rows. It removes its own rows when
# CXFORGE_VERIFY_DB_URL points at the deployment's database, and says
# loudly that it could not when that is absent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/deploy/docker-compose.yml"
CLEANUP_DONE=0

# --- CLI flags -----------------------------------------------------------
# --local: the explicit opt-in required to run the LOCAL
#   (docker-compose-on-this-machine) verification path. See the entrypoint
#   at the bottom of this file for why this can't default on.
# --deep:  the explicit opt-in required to run the core-loop check (W3-G2).
#   Opt-in rather than default for two reasons, in order of weight.
#   (1) It is not free or side-effect-free: it drives a real agent run, so
#   it spends real model tokens and posts a real reply on the helpdesk
#   ticket it is pointed at. (2) It needs preconditions the four liveness
#   assertions do not — a signing secret and a real, fetchable ticket id —
#   and turning it on by default would mean either a hard failure for
#   every existing caller, or a silent skip. Both are worse than a flag.
#   What is NOT acceptable, and is why the PASS lines below were changed
#   in the same commit: letting a run WITHOUT this flag read as though the
#   core loop had been verified.
LOCAL_MODE_OPT_IN=0
DEEP_OPT_IN=0
for arg in "$@"; do
  case "$arg" in
    --local)
      LOCAL_MODE_OPT_IN=1
      ;;
    --deep)
      DEEP_OPT_IN=1
      ;;
    *)
      echo "[verify_deploy] unrecognized argument: $arg" >&2
      exit 1
      ;;
  esac
done

# --- source .env if present, so a human doesn't have to remember
# `set -a; source .env; set +a` themselves (matches the idiom
# .env.example already documents for doctl). ---
#
# Precedence: a DEPLOY_HOST the caller already exported before invoking
# this script must ALWAYS win over whatever .env says — including when
# .env defines DEPLOY_HOST as a bare empty assignment (the shape
# .env.example ships by default). `set -a; source .env` auto-exports
# every assignment .env makes, in THIS shell — so without this
# capture/restore, .env's own `DEPLOY_HOST=` silently clobbers an
# exported value before line 54's `${DEPLOY_HOST:-}` ever runs, because
# by then the overwrite has already happened and there's nothing left to
# fall back from. `${DEPLOY_HOST-sentinel}` (no colon) fires only when
# DEPLOY_HOST is genuinely UNSET, not merely empty, so it distinguishes
# "caller exported DEPLOY_HOST" (even as "") from "caller never touched
# it" — the distinction that matters here.
DEPLOY_HOST_PRE_ENV="${DEPLOY_HOST-__T17_UNSET__}"

if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$REPO_ROOT/.env"
  set +a
  echo "[verify_deploy] sourced $REPO_ROOT/.env"
else
  echo "[verify_deploy] no .env at repo root — proceeding with whatever is already exported"
fi

# Restore precedence: an export from the caller always wins over .env.
if [ "$DEPLOY_HOST_PRE_ENV" != "__T17_UNSET__" ]; then
  DEPLOY_HOST="$DEPLOY_HOST_PRE_ENV"
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

# --- deep-check preconditions ------------------------------------------
# Checked HERE, before a single request goes out, and as hard failures.
# The alternative — discovering halfway through that the deep check cannot
# run and continuing anyway — is the "skip that still exits 0" this
# script's header refuses to have.
DEEP_CHECK_SCRIPT="$REPO_ROOT/scripts/verify_core_loop.py"
if [ "$DEEP_OPT_IN" -eq 1 ]; then
  if [ -z "${ZENDESK_WEBHOOK_SIGNING_SECRET:-}" ]; then
    fail "--deep needs ZENDESK_WEBHOOK_SIGNING_SECRET to sign the synthetic webhook with, and it is empty. It must be the SAME secret the deployment under test was started with, or the endpoint answers 401. Set it in .env (repo root). Refusing to skip the check and exit 0."
  fi
  if [ -z "${CXFORGE_VERIFY_TICKET_ID:-}" ]; then
    fail "--deep needs CXFORGE_VERIFY_TICKET_ID: the id of a disposable helpdesk ticket the DEPLOYED agent can really fetch. A made-up id cannot work — agent.nodes.ingest's first statement is port.fetch_ticket(), so a 404 there fails the run before any 'runs' row is written, and this check would then fail identically whether or not the core loop is connected. See scripts/verify_core_loop.py's module docstring."
  fi
  [ -f "$DEEP_CHECK_SCRIPT" ] || fail "--deep was passed but $DEEP_CHECK_SCRIPT does not exist."
  command -v uv >/dev/null 2>&1 || fail "--deep needs 'uv' on PATH to run $DEEP_CHECK_SCRIPT."
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

  if [ "$DEEP_OPT_IN" -eq 1 ]; then
    run_deep_check "$base"
  else
    echo "[verify_deploy] deep core-loop check: NOT RUN (--deep not passed). The four assertions above are liveness only — they pass against a deployment that answers HTTP and never runs an agent."
  fi
}

# --- deep core-loop check (W3-G2) --------------------------------------
#
# Delegated to Python rather than written in bash, for one reason that
# matters: it signs the request by importing ingress.signature — the SAME
# module the server verifies with. An openssl one-liner here would be a
# private second copy of the HMAC recipe, and the two would be free to
# drift; the check would then be testing its own copy, not the server's.
# (ingress/signature.py's own docstring records what that class of drift
# already cost once: a base64-decoded key that failed every real request
# with 401 while the unit tests, which minted their own base64-valid
# secret, stayed green.)
run_deep_check() {
  local base="$1"
  echo "[verify_deploy] DEEP: POST a signed synthetic webhook and wait for a NEW runs row."
  ( cd "$REPO_ROOT" && uv run python "$DEEP_CHECK_SCRIPT" --base-url "$base" ) \
    || fail "the deep core-loop check did not pass against $base (its own output above says why). The four liveness assertions passing while this fails is the documented signature of a severed core loop — docs/STATE.md §6.2."
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
#
# An exported/`.env`-set DEPLOY_HOST always wins, regardless of --local.
# LOCAL mode is explicit opt-in ONLY (--local): T-11's droplet criterion
# can only be satisfied by a REMOTE-mode run, so an empty DEPLOY_HOST
# with no --local flag must be a hard failure, not a silent fall-through
# to LOCAL that still prints PASS.


# Every PASS below is followed by a line saying what it does NOT cover.
# That is not decoration. `docs/STATE.md §6.2` records that this script's
# unqualified PASS is what carried the claim "the deploy works" for weeks
# across a stack with a dead core loop — so the scope of a pass now travels
# with the pass itself, on the next line, and cannot be left behind when
# somebody quotes the green.
scope_note() {
  if [ "$DEEP_OPT_IN" -eq 1 ]; then
    echo "[verify_deploy] SCOPE: liveness (4/4) AND the deep core-loop check — a signed webhook produced a new runs row on this deployment."
  else
    echo "[verify_deploy] SCOPE: liveness only. The core loop (signed webhook -> Redis -> arq worker -> run_agent -> a runs row) was NOT exercised. Re-run with --deep to check it; until then this pass says the deployment answers HTTP, not that it works."
  fi
}

if [ -n "$DEPLOY_HOST" ]; then
  verify_remote
  echo "[verify_deploy] PASS (REMOTE: verified droplet at $DEPLOY_HOST)"
  scope_note
elif [ "$LOCAL_MODE_OPT_IN" -eq 1 ]; then
  verify_local
  echo "[verify_deploy] LOCAL-MODE PASS — this verified the stack on THIS machine only, NOT a droplet. It does not satisfy T-11's droplet criterion."
  scope_note
else
  fail "DEPLOY_HOST is empty and --local was not passed. This script verifies a REMOTE droplet by default (T-11's droplet criterion can only be met by a remote-mode run). Pass --local only if you intentionally want to check the LOCAL docker-compose stack on this machine — that is NOT droplet evidence."
fi
