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
# THE PUBLIC PATH (2026-08-17). Read this before quoting any pass above.
# Everything described so far — all four liveness assertions AND --deep —
# talks to ${DEPLOY_SCHEME}://${DEPLOY_HOST}:${DEPLOY_PORT}, i.e. the
# droplet's own published port. **Zendesk cannot reach that address.** It
# reaches this app only through ${PUBLIC_BASE_URL} (the Cloudflare hostname
# the tunnel terminates), and a droplet-port request bypasses Cloudflare
# entirely. So the two scopes above are structurally blind to every failure
# that lives in the transport, which is not hypothetical: on 2026-08-17 the
# public path returned **502 for ~64% of real Zendesk deliveries** —
# Cloudflare's edge routing to a prior connector's dead connections, the
# requests never reaching the droplet at all — while this script would have
# reported 4/4 and --deep would have passed (docs/BUILD-PLAN.md §10.6g).
#
# The public-path stage closes that. It runs by DEFAULT in remote mode
# whenever PUBLIC_BASE_URL is set, because a check that only fires when
# somebody remembers a flag would have been absent for exactly the incident
# that motivated it. Three things about how it is built:
#
#   1. It SAMPLES. The failure it exists to catch is probabilistic and
#      varies by which Cloudflare colo a request enters, so one 200 proves
#      almost nothing: a 64%-failure outage survives a single request 36% of
#      the time, and 20 requests 1.3e-9 of the time. Every sample is its own
#      `curl` process — a new TCP connection, no keep-alive reuse — so
#      samples can land on different edge connections. It reports the RATE,
#      not a boolean, and any sample missing its expected status fails the
#      stage.
#   2. It probes the ZENDESK ENDPOINT, not just /health. POST
#      /webhooks/zendesk with an UNSIGNED body must answer 401. That is a
#      pure read: `ingress.receive_zendesk_webhook` verifies the signature
#      before it touches the body, the database or the queue, so nothing is
#      written and no run is dispatched — and a 401 is positive proof the
#      request reached the application, where a 502/530/000 proves it did
#      not. Checking only /health would leave a per-path Cloudflare rule or
#      a mis-pointed public hostname invisible.
#   3. An unset PUBLIC_BASE_URL SKIPS LOUDLY and says so again on the
#      SCOPE: line — never a silent pass. `--public` turns that skip into a
#      hard failure, for callers (CI, a release gate) that need the public
#      path checked or nothing.
#
# Two ways this stage could be turned into a false green, both closed:
# pointing PUBLIC_BASE_URL at loopback is labelled SIMULATED in the pass
# line and on the SCOPE: line, and CXFORGE_PUBLIC_SAMPLES below has an
# enforced floor so the check cannot be "fixed" by sampling zero or one
# request.
#
# It does not replace --deep and does not duplicate it: --deep proves the
# loop runs, this proves the transport carries. To get both through the real
# hostname in one run, point the whole script at it — the public origin
# serves the portal, /api/* and /webhooks/* (measured 2026-08-17):
#   DEPLOY_SCHEME=https DEPLOY_HOST=cxforge.hankholcomb.com DEPLOY_PORT=443 \
#     bash scripts/verify_deploy.sh --deep
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
#   bash scripts/verify_deploy.sh                 # REMOTE (DEPLOY_HOST), liveness
#                                                 # + the public path if
#                                                 # PUBLIC_BASE_URL is set
#   bash scripts/verify_deploy.sh --local         # LOCAL docker-compose stack
#   bash scripts/verify_deploy.sh --public        # public path REQUIRED, not skippable
#   CXFORGE_VERIFY_TICKET_ID=<id> \
#     bash scripts/verify_deploy.sh --deep        # liveness + the core loop
#
# Env knobs for the public-path stage:
#   PUBLIC_BASE_URL            the Cloudflare hostname (from .env; an export wins)
#   CXFORGE_PUBLIC_SAMPLES     samples per probe (default 20, floor 4)
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
#   --public: makes the public-path stage MANDATORY. It is not the opt-in for
#   running that stage — a remote run with PUBLIC_BASE_URL set runs it either
#   way, because the whole defect being fixed is a gate that was blind by
#   default. What this flag changes is the consequence of not being able to
#   run it: without the flag, an empty PUBLIC_BASE_URL is a loud skip and the
#   run can still pass; with it, an empty PUBLIC_BASE_URL is a hard failure
#   before a single request goes out, the same way --deep treats a missing
#   signing secret. It also forces the stage in LOCAL mode, where it is
#   otherwise skipped because the public hostname fronts the droplet and has
#   nothing to do with the stack on this machine.
LOCAL_MODE_OPT_IN=0
DEEP_OPT_IN=0
PUBLIC_REQUIRED_OPT_IN=0
for arg in "$@"; do
  case "$arg" in
    --local)
      LOCAL_MODE_OPT_IN=1
      ;;
    --deep)
      DEEP_OPT_IN=1
      ;;
    --public)
      PUBLIC_REQUIRED_OPT_IN=1
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

# PUBLIC_BASE_URL needs the identical treatment, and for a sharper reason:
# `.env.example` ships it as a bare empty assignment too, so without this an
# exported PUBLIC_BASE_URL would be clobbered to "" by sourcing .env and the
# public-path stage would silently SKIP — the T-17 clobber bug, reproduced
# against the one check that exists because the gate was blind. Same
# no-colon form, same reason: distinguish "exported, even as empty" from
# "never set".
PUBLIC_BASE_URL_PRE_ENV="${PUBLIC_BASE_URL-__PUBLIC_UNSET__}"

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
if [ "$PUBLIC_BASE_URL_PRE_ENV" != "__PUBLIC_UNSET__" ]; then
  PUBLIC_BASE_URL="$PUBLIC_BASE_URL_PRE_ENV"
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

# The Cloudflare hostname Zendesk delivers to. Trailing slash stripped so
# "$PUBLIC_BASE_URL/health" can never become a "//health" that some proxies
# answer differently from the path the webhook actually uses.
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL%/}"
PUBLIC_SAMPLES="${CXFORGE_PUBLIC_SAMPLES:-20}"
# The floor is not decoration and is not a style preference. This stage
# exists to catch a PARTIAL outage; with n samples a p-failure outage evades
# it with probability (1-p)^n, so at the measured p=0.64 one sample misses it
# 36% of the time. A knob that can be set to 0 or 1 is a knob that can turn a
# red gate green without fixing anything — the precise move this whole file
# is a monument to — so the floor is enforced rather than documented.
MIN_PUBLIC_SAMPLES=4

# The path Zendesk actually POSTs to (ingress's router prefix + route).
PUBLIC_WEBHOOK_PATH="/webhooks/zendesk"

# What the SCOPE: line will say about the public path. Overwritten by the
# stage; this default is what a run that never reached it reports, so a
# code path that forgets to set it still cannot read as "checked".
PUBLIC_SCOPE="NOT CHECKED — the public-path stage did not run."

# The address the four liveness assertions ran against; set by run_assertions
# and named on the SCOPE: line so a reader cannot mistake which path passed.
ASSERTED_BASE=""

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

# --- public-path preconditions -----------------------------------------
# Same discipline as --deep's block above: checked here, before any request,
# as hard failures.
case "$PUBLIC_SAMPLES" in
  '' | *[!0-9]*)
    fail "CXFORGE_PUBLIC_SAMPLES must be a positive integer, got '$PUBLIC_SAMPLES'. Refusing to guess: a value this script could not parse would silently become 0 samples, and a stage that sends no requests passes unconditionally."
    ;;
esac
if [ "$PUBLIC_SAMPLES" -lt "$MIN_PUBLIC_SAMPLES" ]; then
  fail "CXFORGE_PUBLIC_SAMPLES=$PUBLIC_SAMPLES is below the floor of $MIN_PUBLIC_SAMPLES. The public-path failure this stage exists to catch is probabilistic (measured: 502 on ~64% of deliveries), so a handful of samples is the minimum that can distinguish 'serving' from 'serving sometimes'. One 200 proves almost nothing."
fi
if [ "$PUBLIC_REQUIRED_OPT_IN" -eq 1 ] && [ -z "$PUBLIC_BASE_URL" ]; then
  fail "--public was passed but PUBLIC_BASE_URL is empty. That variable names the Cloudflare hostname Zendesk delivers to, and it is the ONLY route into this app from Zendesk — the droplet-port assertions cannot substitute for it. Set it in .env (repo root), or drop --public and accept a run that says out loud it did not check the public path."
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
  # Recorded globally so the SCOPE: line can name the address that was
  # actually asserted against, rather than leaving the reader to infer it.
  ASSERTED_BASE="$base"
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

# --- public-path stage -------------------------------------------------
#
# Written in bash with `curl` rather than delegated to Python like --deep,
# deliberately: this stage must be able to run when the application's own
# dependencies cannot be imported, and it borrows nothing from the code under
# test — its whole subject is the network in front of that code. The one thing
# it does share with the app is the endpoint path, and that is a literal here
# (`PUBLIC_WEBHOOK_PATH`) because a wrong path shows up immediately as a 404
# in the histogram rather than as a false pass.

# Results of the last `sample_endpoint` call. Globals because bash functions
# return a status, not a tuple; read immediately by the caller.
SAMPLE_OK=0
SAMPLE_CODES=""

# sample_endpoint METHOD URL EXPECTED N [EXTRA_CURL_ARGS...]
#
# One `curl` process per sample, on purpose. A single curl with --repeat or a
# persistent connection would reuse one TCP/TLS session and therefore one
# Cloudflare edge connection — which is precisely the variable that made the
# 2026-08-17 outage look intermittent. Separate processes let successive
# samples land on different edge connections and (over the anycast IPs) can
# land on different colos.
sample_endpoint() {
  local method="$1" url="$2" expected="$3" n="$4"
  shift 4
  local i code
  SAMPLE_OK=0
  SAMPLE_CODES=""
  for ((i = 1; i <= n; i++)); do
    # `|| true` inside the substitution, so a curl that cannot connect still
    # contributes its write-out ("000") instead of aborting the run under
    # `set -e`. A connection refused / DNS failure / TLS error is a DATA
    # POINT for this stage, not an error in it.
    code="$(curl -sS -o /dev/null -w '%{http_code}' -X "$method" --max-time 15 "$@" "$url" 2>/dev/null || true)"
    [ -n "$code" ] || code="000"
    SAMPLE_CODES="$SAMPLE_CODES $code"
    if [ "$code" = "$expected" ]; then
      SAMPLE_OK=$((SAMPLE_OK + 1))
    fi
  done
}

# "200 502 200" -> "200 x2, 502 x1". Sorted by frequency so the dominant
# answer reads first. Deliberately shows every distinct status: "502 x13" and
# "000 x13" are different diagnoses (edge reached, origin dead vs. never
# reached Cloudflare at all).
code_histogram() {
  # Unquoted on purpose: the word splitting IS the parse.
  # shellcheck disable=SC2086
  printf '%s\n' $1 | sort | uniq -c | sort -rn |
    awk '{printf "%s%s x%s", (NR > 1 ? ", " : ""), $2, $1} END {printf "\n"}'
}

# Integer-safe percentage without bc (not present on a stock droplet).
percent() {
  awk -v ok="$1" -v total="$2" 'BEGIN {printf "%.1f", (total > 0) ? (100 * ok) / total : 0}'
}

run_public_check() {
  local base="$PUBLIC_BASE_URL" n="$PUBLIC_SAMPLES" label="the real hostname, through Cloudflare"
  case "$base" in
    *://localhost* | *://127.* | *://[[]::1[]]*)
      # Not refused — this is how the stage is proven able to fail, by
      # pointing it at a server that misbehaves on purpose. But it must
      # never be reportable as evidence about the Zendesk path, so the
      # label travels into the pass line and the SCOPE: line.
      label="SIMULATED — loopback, NOT the real transport"
      ;;
  esac

  echo "[verify_deploy] PUBLIC PATH: $base ($label)"
  echo "[verify_deploy]   this is the only address Zendesk can deliver to; everything above bypassed it."
  echo "[verify_deploy]   sampling ${n}x GET /health (expect 200) and ${n}x POST ${PUBLIC_WEBHOOK_PATH} unsigned (expect 401) ..."

  sample_endpoint GET "$base/health" 200 "$n"
  local health_ok="$SAMPLE_OK" health_codes="$SAMPLE_CODES"

  # Unsigned, so it is a read: ingress verifies the HMAC before it parses the
  # body, writes `tickets_seen` or enqueues anything, and answers 401. The
  # 401 is the point — it can only come from the application.
  sample_endpoint POST "$base$PUBLIC_WEBHOOK_PATH" 401 "$n" \
    -H 'Content-Type: application/json' \
    --data '{"cxforge":"unsigned public-path probe; a 401 here is the expected answer"}'
  local hook_ok="$SAMPLE_OK" hook_codes="$SAMPLE_CODES"

  local health_rate hook_rate health_hist hook_hist summary
  health_rate="$(percent "$health_ok" "$n")"
  hook_rate="$(percent "$hook_ok" "$n")"
  health_hist="$(code_histogram "$health_codes")"
  hook_hist="$(code_histogram "$hook_codes")"

  summary="GET /health ${health_ok}/${n} = ${health_rate}% expected 200 [${health_hist}]; POST ${PUBLIC_WEBHOOK_PATH} ${hook_ok}/${n} = ${hook_rate}% expected 401 [${hook_hist}]"
  printf '[verify_deploy]   %-22s -> %s/%s = %s%% [%s]\n' "GET  /health" "$health_ok" "$n" "$health_rate" "$health_hist"
  printf '[verify_deploy]   %-22s -> %s/%s = %s%% [%s]\n' "POST $PUBLIC_WEBHOOK_PATH" "$hook_ok" "$n" "$hook_rate" "$hook_hist"

  if [ "$health_ok" -eq "$n" ] && [ "$hook_ok" -eq "$n" ]; then
    PUBLIC_SCOPE="CHECKED, all green — ${base} (${label}): ${summary}"
    echo "[verify_deploy] PUBLIC PATH: PASS — all $((n * 2)) requests through $base reached the application."
    return 0
  fi

  PUBLIC_SCOPE="CHECKED and FAILED — ${base} (${label}): ${summary}"
  fail "the PUBLIC path is not serving reliably: ${summary}. That is the only route Zendesk has, and a partial rate is the measured signature of Cloudflare edge connections routed to a dead tunnel connector — docs/BUILD-PLAN.md §10.6g: 502 on ~64% of real deliveries while the droplet's own port answered every request. Recover with 'docker compose -f deploy/docker-compose.yml up -d --force-recreate cloudflared' ON THE DROPLET and re-run this stage; do NOT 'restart' cloudflared (§10.7d) and do not conclude anything from its /ready endpoint or the Cloudflare dashboard — both reported healthy throughout the outage."
}

# public_stage MODE  (MODE is "remote" or "local")
public_stage() {
  local mode="$1"

  if [ -z "$PUBLIC_BASE_URL" ]; then
    # --public already hard-failed above, so reaching here means the caller
    # did not require it. Skip — loudly, in the run output AND again on the
    # SCOPE: line, because the whole failure mode being fixed is a pass that
    # reads as more than it is.
    PUBLIC_SCOPE="NOT CHECKED — PUBLIC_BASE_URL is empty, so nothing in this run touched the hostname Zendesk delivers to. The assertions above targeted ${ASSERTED_BASE:-the target above}, which bypasses Cloudflare and cannot fail when the public path is down. Set PUBLIC_BASE_URL in .env, or pass --public to make this a hard failure."
    echo "[verify_deploy] PUBLIC PATH: NOT CHECKED — PUBLIC_BASE_URL is empty."
    echo "[verify_deploy]   Zendesk reaches this app ONLY through that hostname. Everything above went"
    echo "[verify_deploy]   to ${ASSERTED_BASE:-the target above}, which bypasses Cloudflare entirely, so no"
    echo "[verify_deploy]   assertion in this run can fail when the public path is broken — the exact"
    echo "[verify_deploy]   state in which ~64% of real deliveries 502'd on 2026-08-17 (BUILD-PLAN §10.6g)."
    echo "[verify_deploy]   Set PUBLIC_BASE_URL in .env to check it, or pass --public to make its absence fatal."
    return 0
  fi

  if [ "$mode" = "local" ] && [ "$PUBLIC_REQUIRED_OPT_IN" -eq 0 ]; then
    PUBLIC_SCOPE="NOT CHECKED — LOCAL mode. PUBLIC_BASE_URL ($PUBLIC_BASE_URL) fronts the DROPLET, not the stack on this machine, so checking it here would report on a deployment this run did not touch. Pass --public to check it anyway."
    echo "[verify_deploy] PUBLIC PATH: NOT CHECKED — LOCAL mode; $PUBLIC_BASE_URL fronts the droplet, not this machine's stack. Pass --public to check it anyway."
    return 0
  fi

  run_public_check
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
  public_stage local

  echo "[verify_deploy] all assertions passed against the LOCAL stack."
  echo "[verify_deploy] NOTE: this verified the stack running on THIS machine, not a deployed droplet. DEPLOY_HOST is empty in .env — remote deployment has not been verified."
}

# --- remote mode -----------------------------------------------------------

verify_remote() {
  echo "[verify_deploy] DEPLOY_HOST=$DEPLOY_HOST -> verifying the REMOTE deploy over the network. No local build/start/stop will happen."

  run_assertions "${DEPLOY_SCHEME}://${DEPLOY_HOST}:${DEPLOY_PORT}"
  # Runs by default here — see the --public flag comment for why this stage is
  # not gated behind an opt-in the way --deep is.
  public_stage remote

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
#
# Since 2026-08-17 the SCOPE block answers two independent questions, because
# a pass can be shallow in two unrelated directions and the old single line
# could only say one of them:
#   WHAT was exercised — liveness only, or the core loop as well (--deep); and
#   WHICH PATH it was exercised over — the droplet's own port, which Zendesk
#   cannot reach, or ${PUBLIC_BASE_URL} through Cloudflare, which is the only
#   route it can. A green core-loop check on the droplet port and a 64%-broken
#   public path is a real state this deployment has been in, and the reader
#   must not be able to collapse the two.
scope_note() {
  if [ "$DEEP_OPT_IN" -eq 1 ]; then
    echo "[verify_deploy] SCOPE: liveness (4/4) AND the deep core-loop check — a signed webhook produced a new runs row on this deployment."
  else
    echo "[verify_deploy] SCOPE: liveness only. The core loop (signed webhook -> Redis -> arq worker -> run_agent -> a runs row) was NOT exercised. Re-run with --deep to check it; until then this pass says the deployment answers HTTP, not that it works."
  fi
  echo "[verify_deploy] SCOPE: PATH ASSERTED — ${ASSERTED_BASE:-<none>}. Zendesk cannot reach that address; it bypasses Cloudflare."
  echo "[verify_deploy] SCOPE: PUBLIC PATH (${PUBLIC_BASE_URL:-<unset>}, the only route Zendesk has) — ${PUBLIC_SCOPE}"
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
