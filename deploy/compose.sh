#!/usr/bin/env bash
# `docker compose` for the production stack, with the step nobody remembers.
#
# THE TRAP THIS EXISTS TO CLOSE (docs/deploy.md:151, .claude/NEEDS_HUMAN.md).
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
# Usage — anything you would pass to `docker compose`:
#   deploy/compose.sh config
#   deploy/compose.sh up -d --build --wait
#   deploy/compose.sh logs -f worker
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${CXFORGE_ENV_FILE:-$REPO_ROOT/.env}"
COMPOSE_FILE="$REPO_ROOT/deploy/docker-compose.yml"

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

exec docker compose -f "$COMPOSE_FILE" "$@"
