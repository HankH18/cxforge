#!/usr/bin/env bash
# `docker compose` for the production stack, with the two steps nobody remembers.
#
# TRAP 1 — THE UNSOURCED .env (docs/deploy.md:151, .claude/NEEDS_HUMAN.md).
# `docker compose` reads a `.env` file from the *project directory*, which is
# the directory holding the compose file. For deploy/docker-compose.yml that
# is `deploy/`, and there is no `deploy/.env` — so every `${VAR}` in it falls
# back to its `:-` default and the stack comes up with no Zendesk credentials,
# no Anthropic key, and PORTAL_TOKEN literally `dev-portal-token`. Measured
# 2026-08-16: `docker compose -f deploy/docker-compose.yml config` renders
# ANTHROPIC_API_KEY, all four ZENDESK_* and both LANGFUSE_* keys as the empty
# string, from a repo whose .env has every one of them populated. Nothing
# fails; it just deploys a stack that cannot answer a ticket.
#
# The documented fix is to type `set -a; source .env; set +a` first. That
# works, and it has to be typed correctly every single time by a human who
# already believes the deploy is fine. This script does it instead.
#
# TRAP 2 — cloudflared STARTED FROM A DEVELOPER MACHINE (2026-08-17 outage;
# deploy/cloudflared/README.md, docs/OWNER-ACTIONS.md OA-3). The token in
# CLOUDFLARE_TUNNEL_TOKEN identifies the TUNNEL, not the host, so a
# `cloudflared` started anywhere with that token registers as a SECOND
# CONNECTOR for the same tunnel and can win the edge's routing. On 2026-08-17
# `deploy/compose.sh up -d --force-recreate cloudflared`, typed on a Mac
# instead of on the droplet, took the public site down completely — 10/10
# requests 502 — and `docker stop` on the laptop container did not bring it
# back. The guard below refuses to start `cloudflared` from a machine that is
# not the droplet; everything it knows is written out in the refusal message.
#
# Usage — anything you would pass to `docker compose`:
#   deploy/compose.sh config
#   deploy/compose.sh up -d --build --wait
#   deploy/compose.sh logs -f worker
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${CXFORGE_ENV_FILE:-$REPO_ROOT/.env}"
COMPOSE_FILE="$REPO_ROOT/deploy/docker-compose.yml"

# --- cloudflared guard: constants -----------------------------------------

# The address docs/deploy.md records for this project's droplet (DigitalOcean
# droplet `cxforge`, id 592687747). See this_machine_is_the_droplet() for why
# an address is the identity check and why this literal is safe to carry here.
DROPLET_ADDR_OF_RECORD="161.35.2.250"

# Deliberate override. The name is long and specific because the whole point
# is that it cannot end up set by accident, and the value must match exactly —
# `=1` or `=true` does not enable it.
GUARD_OVERRIDE_VAR="CXFORGE_ALLOW_SECOND_CONNECTOR"
GUARD_OVERRIDE_VALUE="i-know-this-hijacks-the-live-tunnel"

# A second, durable way to say "this host IS the deploy target", for a droplet
# this repo cannot name in advance (a rebuild behind a DigitalOcean reserved
# IP, or a second droplet). Nothing needs it today — the address check below
# already identifies the droplet with no manual step — so it is an escape
# hatch, not a prerequisite.
DROPLET_MARKER="/etc/cxforge-droplet"

# Captured BEFORE the env file is sourced, on purpose. If `.env` could set the
# override, one stale line in one developer's `.env` would silently disable
# this guard for every future invocation on that machine — which is the exact
# shape of failure the guard exists to stop. An override has to be typed at
# the point of use.
GUARD_OVERRIDE_FROM_SHELL="${CXFORGE_ALLOW_SECOND_CONNECTOR-}"

# Filled in by this_machine_is_the_droplet() with the reason it said yes, so
# an allowed run says which fact allowed it instead of just proceeding.
DROPLET_EVIDENCE=""

# Filled in by invocation_starts_cloudflared() with WHICH of its rules fired,
# so the refusal names the specific reason rather than guessing at one.
GUARD_REASON=""

# --- cloudflared guard: does this invocation start the tunnel? -------------

# The service names in the compose file, derived from the file rather than
# hardcoded: a service added later must not read as an unrecognised token.
compose_services() {
  awk '
    /^services:[[:space:]]*$/  { in_services = 1; next }
    /^[^[:space:]#]/           { in_services = 0 }
    in_services && /^  [A-Za-z0-9_.-]+:[[:space:]]*$/ {
      gsub(/[: ]/, "", $0)
      print
    }
  ' "$COMPOSE_FILE"
}

# True when running `docker compose <these args>` would start `cloudflared`.
#
# Two ways it can. Naming it, or naming NOTHING: `up` with no service list
# starts every service in the file, which is how the documented
# `deploy/compose.sh up -d --build --wait` starts the tunnel on the droplet.
# Naming other services does not pull it in — compose starts a named
# service's `depends_on` dependencies, never its dependents, and nothing in
# the file depends on cloudflared.
#
# Where this parser is unsure it answers "yes". Over-refusing costs a
# developer one explicit service list; under-refusing costs a public outage.
invocation_starts_cloudflared() {
  local n=$# i=0 arg subcmd="" named=0 skip_next=0 known_services
  local -a argv
  argv=("$@")

  # Pass 1: the subcommand, skipping the global flags that can precede it and
  # the values those flags take.
  while [ "$i" -lt "$n" ]; do
    arg="${argv[$i]}"
    i=$((i + 1))
    if [ "$skip_next" -eq 1 ]; then
      skip_next=0
      continue
    fi
    case "$arg" in
      --*=*) ;;
      -f | --file | -p | --project-name | --project-directory | --env-file | --profile | --progress | --parallel | --ansi)
        skip_next=1
        ;;
      -*) ;;
      *)
        subcmd="$arg"
        break
        ;;
    esac
  done

  case "$subcmd" in
    # Subcommands that start containers.
    up | start | restart | create | run | scale) ;;
    # Subcommands that start nothing. Listed explicitly rather than inferred
    # from "not a start verb", so that a subcommand this script has never
    # heard of falls into the conservative branch below instead of being
    # waved through.
    config | ps | logs | down | stop | kill | build | pull | push | images | top | port | pause | unpause | exec | cp | events | version | ls | wait | attach | rm | watch | publish | alpha | convert | commit | export | bridge | volumes)
      return 1
      ;;
    *)
      # Unrecognised, including the empty string (no subcommand at all).
      # Do not guess what it does: refuse if a start verb or the service name
      # appears anywhere in the invocation.
      for arg in "$@"; do
        case "$arg" in
          up | start | restart | create | run | scale | *cloudflared*)
            GUARD_REASON="this invocation could not be parsed (subcommand '$subcmd'), and it contains '$arg'"
            return 0
            ;;
        esac
      done
      return 1
      ;;
  esac

  # Pass 2: the service list, from just after the subcommand.
  known_services="$(compose_services)"
  skip_next=0
  while [ "$i" -lt "$n" ]; do
    arg="${argv[$i]}"
    i=$((i + 1))
    if [ "$skip_next" -eq 1 ]; then
      skip_next=0
      continue
    fi
    case "$arg" in
      # Named outright — as a service, as `scale cloudflared=2`, or as the
      # value of a flag like `--attach`. Only reachable under a start verb.
      *cloudflared*)
        GUARD_REASON="'cloudflared' is named in the invocation"
        return 0
        ;;
      --*=*) ;;
      -d | --detach | --build | --no-build | --force-recreate | --no-recreate | --no-deps | --no-start | --wait | --remove-orphans | --abort-on-container-exit | --abort-on-container-failure | --always-recreate-deps | --renew-anon-volumes | -V | --quiet-pull | --no-color | --no-log-prefix | --dry-run | --menu | --watch | --rm | --service-ports | --use-aliases | --no-TTY | -T | -i | --interactive | -t | --tty) ;;
      -*)
        # An option this list does not know. Assume the next token is its
        # value rather than a service name — that keeps a value like the `30`
        # in `up --timeout 30` from passing as "a service was named", which
        # would let an all-services `up` through.
        skip_next=1
        ;;
      *)
        if printf '%s\n' "$known_services" | grep -qxF -- "$arg"; then
          named=1
          if [ "$subcmd" = run ]; then
            # `run [OPTIONS] SERVICE [COMMAND] [ARGS...]`: the service is
            # settled and everything after it is the command, which is not a
            # service list. cloudflared cannot be reached from here — the
            # branch above catches it wherever it appears, and nothing in the
            # compose file depends on it, so no other service pulls it in.
            return 1
          fi
        else
          # A positional this parser cannot place. Refuse to guess.
          GUARD_REASON="'$arg' is not a service in deploy/docker-compose.yml, so which services '$subcmd' would start cannot be read off this invocation"
          return 0
        fi
        ;;
    esac
  done

  if [ "$subcmd" = run ] && [ "$named" -eq 1 ]; then
    # `run` takes exactly one service, and it was not cloudflared.
    return 1
  fi

  if [ "$named" -eq 0 ]; then
    # No service named: compose starts all of them, cloudflared included.
    GUARD_REASON="'$subcmd' names no service, so it starts EVERY service in deploy/docker-compose.yml — cloudflared included"
    return 0
  fi
  return 1
}

# --- cloudflared guard: am I the droplet? ----------------------------------

# Every IPv4 address this machine itself holds, as a text blob. Several
# sources, absolute paths included, because a PATH without `ip` on it must not
# be able to make the droplet look like a laptop.
host_ipv4_addresses() {
  local cmd
  for cmd in ip /usr/sbin/ip /sbin/ip /usr/bin/ip /bin/ip; do
    if command -v -- "$cmd" >/dev/null 2>&1; then
      "$cmd" -4 -o addr show 2>/dev/null || true
    fi
  done
  for cmd in ifconfig /usr/sbin/ifconfig /sbin/ifconfig; do
    if command -v -- "$cmd" >/dev/null 2>&1; then
      "$cmd" -a 2>/dev/null || true
    fi
  done
  hostname -I 2>/dev/null || true
}

# Is $1 an address configured on one of this machine's own interfaces?
#
# Literal IPv4 only, deliberately: no DNS lookup, because a guard that
# resolved a name would vary with the resolver and could hang, and a
# DEPLOY_HOST that is a hostname is already covered by the address of record.
# Loopback, link-local and RFC1918 private addresses never count — every
# laptop holds those, and the deploy target of a public droplet is a public
# address.
host_holds_public_address() {
  local addr="${1:-}" escaped
  case "$addr" in
    "" | *[!0-9.]*) return 1 ;;
    127.* | 0.* | 169.254.* | 10.* | 192.168.*) return 1 ;;
    172.1[6-9].* | 172.2[0-9].* | 172.3[01].*) return 1 ;;
  esac
  escaped="${addr//./\\.}"
  host_ipv4_addresses | grep -qE "(^|[^0-9.])${escaped}([^0-9.]|$)"
}

# Detection, and why it is this and not a hostname or a required marker file:
#
# DigitalOcean assigns a droplet's public IPv4 directly to its `eth0` inside
# the guest — there is no NAT — and the account holds no reserved (floating)
# IPs, checked read-only against the DO API on 2026-08-17: droplet `cxforge`
# (id 592687747) reports 161.35.2.250 type "public", and /v2/reserved_ips is
# empty. So "this machine holds the address the repo deploys to" is true on
# the droplet right now, with nothing installed and no marker placed, and it
# is false on a laptop unless someone deliberately binds that address.
#
# DEPLOY_HOST is read first so a future droplet identifies itself from the
# `.env` that is copied to it anyway (docs/deploy.md §4) — no new step. The
# literal address of record is the fallback for a droplet whose `.env` leaves
# DEPLOY_HOST empty, which `.env.example` ships as the default.
this_machine_is_the_droplet() {
  if [ -f "$DROPLET_MARKER" ]; then
    DROPLET_EVIDENCE="the marker file $DROPLET_MARKER exists"
    return 0
  fi
  local addr
  for addr in "${DEPLOY_HOST:-}" "$DROPLET_ADDR_OF_RECORD"; do
    if host_holds_public_address "$addr"; then
      DROPLET_EVIDENCE="this machine holds $addr, the address this repo deploys to"
      return 0
    fi
  done
  return 1
}

# --- cloudflared guard: what it says --------------------------------------

# The mechanism, stated once and printed on both the refusal and the
# override, because the reason is the part that stops a repeat.
guard_why() {
  cat >&2 <<'EOF'
  WHY — measured on 2026-08-17, not theorised:

    * CLOUDFLARE_TUNNEL_TOKEN identifies the TUNNEL, not the host. Any
      cloudflared started with it registers as a SECOND CONNECTOR for the
      same tunnel, and Cloudflare's edge can route public traffic to it.
    * The ingress config comes DOWN from the Cloudflare dashboard
      (service: http://portal:80). A local stack has no portal container for
      it to reach — 'up -d --force-recreate cloudflared' starts only
      cloudflared's depends_on set (db, redis, backend) — so the edge sends
      real traffic to an origin that does not exist: 10/10 requests 502.
    * STOPPING THE CONTAINER DOES NOT RESTORE SERVICE. 'docker stop' on the
      laptop's cloudflared left the site down; the edge held the stale route.
      Recovery was 'up -d --force-recreate cloudflared' ON THE DROPLET, which
      mints a fresh connector — 15/15 x 200 immediately.
    * /ready and the Cloudflare dashboard stay green through all of it. They
      report this connector's view of itself, not what the edge is routing
      (deploy/cloudflared/README.md, docs/BUILD-PLAN.md 10.6g).
EOF
}

guard_refuse() {
  local invocation="$*"
  {
    echo "deploy/compose.sh: REFUSING to start cloudflared from this machine."
    echo
    echo "  This machine is not the droplet: it holds none of the addresses this"
    echo "  repo deploys to (DEPLOY_HOST=${DEPLOY_HOST:-<empty>}, address of"
    echo "  record $DROPLET_ADDR_OF_RECORD) and $DROPLET_MARKER does not exist."
    echo "  Invocation: docker compose $invocation"
    echo "  Why this counts as starting cloudflared: $GUARD_REASON."
    echo
  } >&2
  guard_why
  {
    echo
    echo "  WHAT TO DO INSTEAD:"
    echo
    echo "    * Start the tunnel where it belongs, on the droplet:"
    echo "        ssh root@${DEPLOY_HOST:-$DROPLET_ADDR_OF_RECORD} \\"
    echo "          'cd ~/othram-support-agent && deploy/compose.sh up -d --build --wait'"
    echo "    * Run the rest of the stack locally by naming the services. This"
    echo "      guard does not touch that invocation:"
    echo "        deploy/compose.sh up -d --wait db redis backend worker portal"
    echo "    * Verify publicly afterwards; the droplet's own port cannot see this"
    echo "      failure at all:"
    printf '%s\n' "        curl -sS -o /dev/null -w '%{http_code}\\n' \"\$PUBLIC_BASE_URL/health\""
    echo
    echo "  IF YOU REALLY MEAN TO RUN A CONNECTOR HERE — it will contend for the"
    echo "  live tunnel's traffic:"
    echo
    echo "    $GUARD_OVERRIDE_VAR=$GUARD_OVERRIDE_VALUE \\"
    echo "      deploy/compose.sh $invocation"
    echo
    echo "  It must be set in the invoking shell. A value coming from $ENV_FILE is"
    echo "  ignored on purpose: an override that could be persisted would disable"
    echo "  this guard silently, forever, on that machine."
  } >&2
  exit 1
}

guard_warn_override() {
  local invocation="$*"
  {
    echo "deploy/compose.sh: WARNING — $GUARD_OVERRIDE_VAR is set, so cloudflared"
    echo "  is being started from a machine that is NOT the droplet."
    echo "  Invocation: docker compose $invocation"
    echo "  Why this counts as starting cloudflared: $GUARD_REASON."
    echo
  } >&2
  guard_why
  {
    echo
    echo "  If the public site goes down after this: stopping this container will"
    echo "  NOT fix it. Run 'deploy/compose.sh up -d --force-recreate cloudflared'"
    echo "  ON THE DROPLET, then read the effect back from the public hostname."
  } >&2
}

# --- the env file, sourced before anything reads DEPLOY_HOST ---------------

if [ ! -f "$ENV_FILE" ]; then
  # Loud and early. Continuing here is precisely the silent-default failure
  # this script exists to prevent, so it is not an option.
  echo "deploy/compose.sh: no env file at $ENV_FILE" >&2
  echo "  The production stack reads every credential from the shell." >&2
  echo "  Copy .env.example to .env and fill it in (docs/deploy.md §4)," >&2
  echo "  or point CXFORGE_ENV_FILE at the file you mean." >&2
  exit 1
fi

# `set -a` exports every assignment the file makes, which is what makes them
# visible to docker compose's ${VAR} interpolation. Anything already exported
# in this shell is overwritten by the file, matching the documented
# `set -a; source .env; set +a` idiom exactly — DEPLOY_HOST's opposite
# precedence rule (.env.example) is a property of verify_deploy.sh, not of
# this one, and is deliberately not reproduced here.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

# --- the guard, before docker is executed ---------------------------------

if invocation_starts_cloudflared "$@"; then
  if this_machine_is_the_droplet; then
    echo "deploy/compose.sh: cloudflared allowed — $DROPLET_EVIDENCE." >&2
  elif [ "$GUARD_OVERRIDE_FROM_SHELL" = "$GUARD_OVERRIDE_VALUE" ]; then
    guard_warn_override "$@"
  else
    guard_refuse "$@"
  fi
fi

exec docker compose -f "$COMPOSE_FILE" "$@"
