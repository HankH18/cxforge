"""Shared fixtures for T-17: drive the REAL ``scripts/verify_deploy.sh`` as
a subprocess against a disposable, synthetic project directory — never the
real repo's ``.env`` (out of scope for T-17, holds secrets) and never real
docker or a real network call.

SAFETY (see the T-17 ticket's hazard note): this machine runs a live
``othram-db`` Postgres container the rest of the suite depends on, plus
two unrelated production droplets on the account's DigitalOcean account.
Every test built on these fixtures:

  * copies the REAL ``scripts/verify_deploy.sh``, byte-for-byte unmodified,
    into a disposable ``tmp_path`` "repo" — the script derives its own
    ``REPO_ROOT`` purely from ``${BASH_SOURCE[0]}`` (its own path), so this
    alone isolates every run from the real repo's ``.env`` with no code
    change and no new env-var seam;
  * puts a fake ``curl`` on ``PATH`` that never opens a socket — it just
    inspects its own argv and prints back the canned status/body text
    ``assert_status``/``assert_body_contains`` expect; and
  * puts either a hostile CANARY ``docker`` (writes a sentinel file and
    fails loudly if ever invoked — used by every test that must prove the
    LOCAL/docker branch was never entered) or a no-op success ``docker``
    stub (used ONLY by the one test that deliberately exercises
    ``--local``, and even then it never starts a real container — it is a
    two-line shell script that just returns 0) on ``PATH``.

No test anywhere in this package ever puts the real ``docker`` binary
first on ``PATH``, and no test ever lets ``curl`` reach a real host.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_SCRIPT = REPO_ROOT / "scripts" / "verify_deploy.sh"

# Env vars the real invoking shell might happen to have exported (e.g. a
# human's own DEPLOY_HOST while debugging) that must NEVER leak into these
# subprocess runs — every test states explicitly, via env_overrides, what
# it wants the child process to see.
_SCRUBBED_ENV_VARS = (
    "DEPLOY_HOST",
    "PORTAL_TOKEN",
    "PORTAL_PORT",
    "BACKEND_PORT",
    "DEPLOY_PORT",
    "DEPLOY_SCHEME",
    # The public-path stage's two inputs. Scrubbed for the usual reason —
    # every test must state what the child sees — and for one specific to
    # them: a developer who ran `set -a; source .env; set +a` in the shell
    # that launched pytest would otherwise hand these tests the REAL
    # Cloudflare hostname, and the public stage would fire against
    # production through the fake curl. The tests would then pass or fail
    # depending on whose shell ran them.
    "PUBLIC_BASE_URL",
    "CXFORGE_PUBLIC_SAMPLES",
)

FAKE_CURL = """#!/usr/bin/env bash
# Fake curl for T-17 deploy tests (backend/tests/deploy). NEVER touches the
# network. Routes purely on the requested URL's suffix (the last
# positional argument scripts/verify_deploy.sh's curl invocations always
# pass) and prints back the canned status/body assert_status /
# assert_body_contains expect from a real deploy.
#
# FAULT INJECTION (added with the public-path stage). Three env vars, all
# optional, let a test make a chosen host answer badly for a chosen fraction
# of requests:
#   FAKE_CURL_FLAKY_MATCH    substring of the URL to afflict (e.g. the public
#                            hostname). Requests that do not match it are
#                            answered normally — which is what lets a test
#                            reproduce the real defect's exact shape: the
#                            droplet port healthy, the public path broken.
#   FAKE_CURL_FLAKY_PATTERN  cycled string of 1 (serve) / 0 (answer 502).
#   FAKE_CURL_FLAKY_STATE    file holding the request counter, so the pattern
#                            is deterministic ACROSS PROCESSES. It has to be:
#                            the stage under test spawns one curl per sample
#                            precisely so samples cannot share a connection,
#                            so no in-process counter could work.
#
# REQUEST JOURNAL. When FAKE_CURL_JOURNAL is set, every invocation appends
# "METHOD URL" to that file. Asserting on the journal is how a test binds
# WHICH endpoint was called, which asserting on the script's own output
# cannot do: the stage prints a label it composes itself, so a stage that
# printed "POST /webhooks/zendesk" while actually requesting /health twice
# was invisible to every output assertion. (Found by sabotage, not by
# reading — the substitution passed all nine tests.)
set -euo pipefail

args=("$@")
n=${#args[@]}
url="${args[$((n - 1))]}"

has_write_out=0
has_token_header=0
method=GET
want_method=0
for a in "${args[@]}"; do
  if [ "$want_method" -eq 1 ]; then
    method="$a"
    want_method=0
    continue
  fi
  case "$a" in
    -X) want_method=1 ;;
    *'%{http_code}'*) has_write_out=1 ;;
    *'X-Portal-Token:'*) has_token_header=1 ;;
  esac
done

if [ -n "${FAKE_CURL_JOURNAL:-}" ]; then
  printf '%s %s\n' "$method" "$url" >> "$FAKE_CURL_JOURNAL"
fi

case "$url" in
  */health)
    body=""
    code=200
    ;;
  */api/metrics)
    if [ "$has_token_header" -eq 1 ]; then
      body='{"ok":true}'
      code=200
    else
      body='{"error":"unauthorized"}'
      code=401
    fi
    ;;
  */webhooks/zendesk)
    # What the real ingress answers an UNSIGNED body: 401, before it parses
    # the body, writes tickets_seen or enqueues anything.
    body='{"detail":"signature verification failed"}'
    code=401
    ;;
  *)
    # Portal index page — vite react-ts template root mount point.
    body='<html><body><div id="root"></div></body></html>'
    code=200
    ;;
esac

if [ -n "${FAKE_CURL_FLAKY_MATCH:-}" ] && [ -n "${FAKE_CURL_FLAKY_STATE:-}" ]; then
  case "$url" in
    *"$FAKE_CURL_FLAKY_MATCH"*)
      seen=0
      if [ -f "$FAKE_CURL_FLAKY_STATE" ]; then
        seen="$(cat "$FAKE_CURL_FLAKY_STATE")"
      fi
      printf '%s' "$((seen + 1))" > "$FAKE_CURL_FLAKY_STATE"
      pattern="${FAKE_CURL_FLAKY_PATTERN:-10}"
      slot="${pattern:$((seen % ${#pattern})):1}"
      if [ "$slot" = "0" ]; then
        code=502
        body='<html><title>502 Bad Gateway</title></html>'
      fi
      ;;
  esac
fi

if [ "$has_write_out" -eq 1 ]; then
  printf '%s' "$code"
else
  printf '%s' "$body"
fi
"""

FAKE_DOCKER_CANARY = """#!/usr/bin/env bash
# Hostile canary for T-17: if this is ever executed, verify_deploy.sh took
# the LOCAL/docker branch when the test asserting on this sentinel
# required it NOT to. Writes a sentinel file the test checks for, and
# fails loudly rather than silently succeeding, so a future regression
# that reintroduces an unwanted docker call can never be masked.
set -euo pipefail
: "${CANARY_SENTINEL:?CANARY_SENTINEL must be set by the test harness}"
echo "docker invoked with: $*" >> "$CANARY_SENTINEL"
echo "FAKE-DOCKER-CANARY: docker must not be invoked here (see T-17 tests)" >&2
exit 111
"""

FAKE_DOCKER_NOOP = """#!/usr/bin/env bash
# No-op success stub for T-17's local-mode test. Never touches real
# docker and never starts a real container — it just returns success for
# the handful of invocations verify_local() makes (`docker info`,
# `docker compose ... up ...`, `docker compose ... down ...`).
set -euo pipefail
exit 0
"""

FAKE_DOCKER_JOURNAL = """#!/usr/bin/env bash
# The no-op stub above, plus a record of WHAT was asked for: every
# invocation appends its own argv, one line per call, to $DOCKER_JOURNAL.
#
# Why a journal and not the script's own output: verify_deploy.sh composes
# its progress lines itself, so a `docker compose up` with the wrong (or no)
# service list prints exactly the same thing as the right one. Asserting on
# the argv is the only way to bind which services a run would really have
# started — and starting `cloudflared` from a machine that is not the droplet
# is the 2026-08-17 outage (deploy/compose.sh TRAP 2), so "it can never be in
# that argv" is worth a test that cannot be fooled by a label.
#
# Starts nothing, ever: it writes a line and exits 0.
set -euo pipefail
: "${DOCKER_JOURNAL:?DOCKER_JOURNAL must be set by the test harness}"
printf '%s\\n' "$*" >> "$DOCKER_JOURNAL"
exit 0
"""


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A disposable repo root containing only a copy of the real
    verify_deploy.sh at scripts/verify_deploy.sh. The script computes its
    own REPO_ROOT from ``${BASH_SOURCE[0]}``, so this alone is enough to
    make it read/write only inside tmp_path — individual tests add a
    synthetic ``.env`` as needed."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    dest = scripts_dir / "verify_deploy.sh"
    shutil.copy2(REAL_SCRIPT, dest)
    return tmp_path


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    """An empty bin/ dir for tests to populate via write_fake_curl /
    write_docker_canary / write_docker_noop, then pass to
    run_verify_deploy(stub_bin_dir=...)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return bin_dir


def write_fake_curl(bin_dir: Path) -> None:
    _write_executable(bin_dir / "curl", FAKE_CURL)


def write_docker_canary(bin_dir: Path, sentinel: Path) -> None:
    """Install the hostile canary. ``sentinel`` must also be passed as
    ``canary_sentinel=`` to run_verify_deploy so the child process knows
    where to write it."""
    _write_executable(bin_dir / "docker", FAKE_DOCKER_CANARY)
    if sentinel.exists():
        sentinel.unlink()


def write_docker_noop(bin_dir: Path) -> None:
    _write_executable(bin_dir / "docker", FAKE_DOCKER_NOOP)


def write_docker_journal(bin_dir: Path, journal: Path) -> None:
    """Install the journaling no-op ``docker``. ``journal`` must also be
    passed to the child as ``DOCKER_JOURNAL`` (via ``env_overrides``) — the
    stub refuses to run without it, so a test that forgets fails loudly
    instead of silently observing an empty journal."""
    _write_executable(bin_dir / "docker", FAKE_DOCKER_JOURNAL)
    if journal.exists():
        journal.unlink()


def run_verify_deploy(
    fake_repo: Path,
    *,
    args: list[str] | None = None,
    env_overrides: dict[str, str] | None = None,
    stub_bin_dir: Path | None = None,
    canary_sentinel: Path | None = None,
    timeout: float = 15,
) -> subprocess.CompletedProcess[str]:
    """Run the (copied) real verify_deploy.sh as a subprocess.

    Starts from a SCRUBBED copy of this test process's own environment (so
    a human's own exported DEPLOY_HOST/PORTAL_TOKEN, if any, can never
    leak into a test) and layers ``env_overrides`` on top. When
    ``stub_bin_dir`` is given it is prepended to PATH so fake curl/docker
    shadow the real ones for this invocation only.
    """
    script = fake_repo / "scripts" / "verify_deploy.sh"
    env = dict(os.environ)
    for key in _SCRUBBED_ENV_VARS:
        env.pop(key, None)
    if stub_bin_dir is not None:
        env["PATH"] = f"{stub_bin_dir}:{env.get('PATH', '')}"
    if canary_sentinel is not None:
        env["CANARY_SENTINEL"] = str(canary_sentinel)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(script), *(args or [])],
        cwd=fake_repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
