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
)

FAKE_CURL = """#!/usr/bin/env bash
# Fake curl for T-17 deploy tests (backend/tests/deploy). NEVER touches the
# network. Routes purely on the requested URL's suffix (the last
# positional argument scripts/verify_deploy.sh's curl invocations always
# pass) and prints back the canned status/body assert_status /
# assert_body_contains expect from a real deploy.
set -euo pipefail

args=("$@")
n=${#args[@]}
url="${args[$((n - 1))]}"

has_write_out=0
has_token_header=0
for a in "${args[@]}"; do
  case "$a" in
    *'%{http_code}'*) has_write_out=1 ;;
    *'X-Portal-Token:'*) has_token_header=1 ;;
  esac
done

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
  *)
    # Portal index page — vite react-ts template root mount point.
    body='<html><body><div id="root"></div></body></html>'
    code=200
    ;;
esac

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
