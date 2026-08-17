"""The cloudflared guard in ``deploy/compose.sh`` (2026-08-17 outage).

WHAT THIS BINDS. On 2026-08-17 ``deploy/compose.sh up -d --force-recreate
cloudflared`` was typed on a Mac instead of on the droplet and took the public
site down completely — 10/10 requests 502. ``CLOUDFLARE_TUNNEL_TOKEN``
identifies the *tunnel*, not the host, so the laptop registered as a second
connector for the live tunnel, won the edge's routing, and had no ``portal``
container to reach. ``docker stop`` did not restore service; only a
force-recreate ON THE DROPLET did. The guard refuses to start ``cloudflared``
from a machine that is not the droplet, and these tests fail if it is removed,
weakened to a warning, or narrowed so that a bare ``up`` slips past.

SAFETY — no test here starts a container, and none can:

  * every run puts a stub ``docker`` first on ``PATH`` that only appends its
    own argv to a journal file and exits 0, so "the guard let this through" is
    observed as a journal line rather than as a running tunnel;
  * every run points ``CXFORGE_ENV_FILE`` at a synthetic env file in
    ``tmp_path``, so the real repo ``.env`` (and the real
    ``CLOUDFLARE_TUNNEL_TOKEN``) is never sourced;
  * the one test that needs the machine to *look* like the droplet stubs the
    OS address query (``ip``) rather than giving the script an env-var bypass —
    there is no such bypass, deliberately.

The refusal tests assert that this machine is NOT the droplet, and skip if it
is: on the droplet the guard is supposed to allow the tunnel, and a test that
asserted otherwise would be asserting the wrong thing there.
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_WRAPPER = REPO_ROOT / "deploy" / "compose.sh"
COMPOSE_FILE = REPO_ROOT / "deploy" / "docker-compose.yml"
CLOUDFLARED_DOC = REPO_ROOT / "deploy" / "cloudflared" / "README.md"
DEPLOY_DOC = REPO_ROOT / "docs" / "deploy.md"
OWNER_ACTIONS = REPO_ROOT / "docs" / "OWNER-ACTIONS.md"
BUILD_PLAN = REPO_ROOT / "docs" / "BUILD-PLAN.md"

# Read out of the script rather than hardcoded, so renaming the override or
# repointing the droplet fails the consistency tests below instead of leaving
# these tests quietly exercising a spelling nothing uses any more.
WRAPPER_TEXT = COMPOSE_WRAPPER.read_text()


def _wrapper_constant(name: str) -> str:
    match = re.search(rf'^{name}="([^"]*)"', WRAPPER_TEXT, re.MULTILINE)
    assert match, f"deploy/compose.sh no longer defines {name}"
    return match.group(1)


DROPLET_ADDR = _wrapper_constant("DROPLET_ADDR_OF_RECORD")
OVERRIDE_VAR = _wrapper_constant("GUARD_OVERRIDE_VAR")
OVERRIDE_VALUE = _wrapper_constant("GUARD_OVERRIDE_VALUE")
DROPLET_MARKER = _wrapper_constant("DROPLET_MARKER")

STUB_DOCKER = """#!/usr/bin/env bash
# Stub docker for the cloudflared-guard tests. Starts nothing, ever: it
# appends its own argv to $DOCKER_JOURNAL and exits 0. A journal line is the
# test's evidence that the guard let an invocation through.
set -euo pipefail
printf '%s\\n' "$*" >> "$DOCKER_JOURNAL"
exit 0
"""

STUB_IP_TEMPLATE = """#!/usr/bin/env bash
# Stub `ip` that reports ONE synthetic address, so a test can make this
# machine look like the droplet (or like a laptop on a LAN) without giving the
# guard an env-var bypass. Mimics `ip -4 -o addr show` output.
printf '%s\\n' "2: eth0    inet {addr}/20 brd 10.0.0.255 scope global eth0"
"""


def _this_machine_holds(addr: str) -> bool:
    """Does this machine actually hold ``addr`` on one of its interfaces?

    Asked with the same sources the guard uses. Only used to state a test's
    premise — never as the assertion.
    """
    blob = ""
    for cmd in (
        ["ip", "-4", "-o", "addr", "show"],
        ["/usr/sbin/ip", "-4", "-o", "addr", "show"],
        ["/sbin/ip", "-4", "-o", "addr", "show"],
        ["ifconfig", "-a"],
        ["/sbin/ifconfig", "-a"],
        ["/usr/sbin/ifconfig", "-a"],
        ["hostname", "-I"],
    ):
        try:
            blob += subprocess.run(
                cmd, capture_output=True, text=True, timeout=15
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
    return re.search(rf"(?<![0-9.]){re.escape(addr)}(?![0-9.])", blob) is not None


ON_THE_DROPLET = _this_machine_holds(DROPLET_ADDR) or Path(DROPLET_MARKER).exists()

not_on_the_droplet = pytest.mark.skipif(
    ON_THE_DROPLET,
    reason=(
        "this machine IS the deploy target (holds "
        f"{DROPLET_ADDR} or has {DROPLET_MARKER}), where the guard is supposed "
        "to allow cloudflared"
    ),
)


class GuardRun:
    def __init__(self, result: subprocess.CompletedProcess[str], journal: Path) -> None:
        self.result = result
        self.journal = journal

    @property
    def stderr(self) -> str:
        return self.result.stderr

    @property
    def returncode(self) -> int:
        return self.result.returncode

    @property
    def docker_argv(self) -> list[str]:
        if not self.journal.exists():
            return []
        return self.journal.read_text().splitlines()

    @property
    def reached_docker(self) -> bool:
        return bool(self.docker_argv)

    def __str__(self) -> str:  # shown on assertion failure
        return (
            f"rc={self.returncode}\n"
            f"--- stderr ---\n{self.result.stderr}\n"
            f"--- stdout ---\n{self.result.stdout}\n"
            f"--- docker invocations ---\n{self.docker_argv}\n"
        )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def run_wrapper(
    tmp_path: Path,
    args: list[str],
    *,
    env_lines: str = f"DEPLOY_HOST={DROPLET_ADDR}\n",
    shell_env: dict[str, str] | None = None,
    fake_local_addr: str | None = None,
) -> GuardRun:
    """Drive the REAL deploy/compose.sh with a stub docker on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    journal = tmp_path / "docker-journal.txt"
    _write_executable(bin_dir / "docker", STUB_DOCKER)
    if fake_local_addr is not None:
        _write_executable(
            bin_dir / "ip", STUB_IP_TEMPLATE.format(addr=fake_local_addr)
        )

    env_file = tmp_path / "synthetic.env"
    env_file.write_text(env_lines)

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CXFORGE_ENV_FILE": str(env_file),
        "DOCKER_JOURNAL": str(journal),
    }
    if shell_env:
        env.update(shell_env)

    result = subprocess.run(
        ["bash", str(COMPOSE_WRAPPER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return GuardRun(result, journal)


# --------------------------------------------------------------------------
# The refusal — the invocation that actually caused the outage
# --------------------------------------------------------------------------


@not_on_the_droplet
def test_the_outage_invocation_is_refused_before_docker_is_reached(
    tmp_path: Path,
) -> None:
    """`up -d --force-recreate cloudflared`, typed on a laptop, 2026-08-17."""
    run = run_wrapper(tmp_path, ["up", "-d", "--force-recreate", "cloudflared"])

    assert run.returncode != 0, str(run)
    assert not run.reached_docker, (
        "the guard let the outage invocation through to docker — a real "
        f"cloudflared would have registered a second connector.\n{run}"
    )


@not_on_the_droplet
def test_the_refusal_explains_the_mechanism_not_just_the_rule(tmp_path: Path) -> None:
    """A refusal nobody understands gets overridden, so the message carries
    the three facts that make the rule non-obvious.

    Asserted on meaning-bearing terms rather than a quoted sentence: rewording
    the prose is fine, dropping the explanation is not.
    """
    run = run_wrapper(tmp_path, ["up", "-d", "--force-recreate", "cloudflared"])
    stderr = run.stderr
    lowered = stderr.lower()

    # 1. the token is tunnel-scoped, so this is not a local-only action
    assert "CLOUDFLARE_TUNNEL_TOKEN" in stderr, str(run)
    assert "tunnel, not the host" in lowered or "not the host" in lowered, str(run)
    # 2. a second connector takes over the edge's routing
    assert "second connector" in lowered, str(run)
    assert "rout" in lowered, str(run)
    # 3. stopping the container does not restore service
    assert "does not restore service" in lowered, str(run)
    assert "force-recreate" in lowered and "droplet" in lowered, str(run)
    # and the way out, spelled exactly as the shell needs it
    assert OVERRIDE_VAR in stderr, str(run)
    assert OVERRIDE_VALUE in stderr, str(run)


@not_on_the_droplet
def test_a_bare_up_is_refused_because_it_starts_every_service(tmp_path: Path) -> None:
    """`up -d --build --wait` names no service, so it starts cloudflared too.

    This is the documented droplet invocation (docs/deploy.md §5,
    deploy/cloudflared/README.md), which is exactly why it must be refused on
    a machine that is not the droplet. A guard that only matched the literal
    word `cloudflared` would miss the outage's larger sibling.
    """
    run = run_wrapper(tmp_path, ["up", "-d", "--build", "--wait"])

    assert run.returncode != 0, str(run)
    assert not run.reached_docker, str(run)
    assert "every service" in run.stderr.lower(), str(run)


@not_on_the_droplet
@pytest.mark.parametrize(
    "args",
    [
        ["up"],
        ["up", "-d"],
        ["up", "-d", "--timeout", "30"],  # option value must not read as a service
        ["start"],
        ["restart", "cloudflared"],
        ["create"],
        ["scale", "cloudflared=2"],
        ["run", "--rm", "cloudflared", "sh"],
    ],
)
def test_every_way_of_starting_the_tunnel_is_refused(
    tmp_path: Path, args: list[str]
) -> None:
    run = run_wrapper(tmp_path, args)
    assert run.returncode != 0, str(run)
    assert not run.reached_docker, str(run)


# --------------------------------------------------------------------------
# Everything else is untouched
# --------------------------------------------------------------------------


def test_the_normal_deploy_invocation_is_untouched(tmp_path: Path) -> None:
    """`up -d --wait db redis backend worker portal` — the invocation the
    local stack is actually brought up with. The guard must not cost it
    anything."""
    args = ["up", "-d", "--wait", "db", "redis", "backend", "worker", "portal"]
    run = run_wrapper(tmp_path, args)

    assert run.returncode == 0, str(run)
    assert run.reached_docker, "the guard blocked the normal deploy invocation"
    assert run.docker_argv[0].endswith(" ".join(args)), str(run)
    assert "REFUS" not in run.stderr.upper(), str(run)


@pytest.mark.parametrize(
    "args",
    [
        ["config"],
        ["config", "--quiet"],
        ["ps"],
        ["logs", "-f", "worker"],
        ["logs", "--tail", "30", "cloudflared"],  # reading its logs is not starting it
        ["down", "-v", "--remove-orphans"],
        ["stop", "cloudflared"],
        ["kill", "cloudflared"],
        ["build", "portal"],
        ["up", "-d", "--build", "portal"],
        ["run", "--rm", "backend", "python", "-c", "print(1)"],
        ["exec", "backend", "ls"],
    ],
)
def test_subcommands_that_do_not_start_the_tunnel_reach_docker(
    tmp_path: Path, args: list[str]
) -> None:
    run = run_wrapper(tmp_path, args)
    assert run.returncode == 0, str(run)
    assert run.reached_docker, str(run)


def test_a_missing_env_file_still_fails_first(tmp_path: Path) -> None:
    """The older trap keeps its precedence: no credentials is reported as
    such, not as a cloudflared refusal."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "docker", STUB_DOCKER)
    result = subprocess.run(
        ["bash", str(COMPOSE_WRAPPER), "up", "-d", "cloudflared"],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "CXFORGE_ENV_FILE": str(tmp_path / "absent.env"),
            "DOCKER_JOURNAL": str(tmp_path / "journal.txt"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1, result.stderr
    assert "no env file" in result.stderr, result.stderr


# --------------------------------------------------------------------------
# The droplet must keep working — the failure mode that would break a deploy
# --------------------------------------------------------------------------


def test_the_tunnel_is_allowed_on_a_machine_that_holds_the_deploy_address(
    tmp_path: Path,
) -> None:
    """The guard's detection is "do I hold the address this repo deploys to".

    Simulated by stubbing the OS address query, not by an env-var bypass —
    there is none. If this test goes red, the next real deploy of the tunnel
    is broken, which is the expensive direction of this guard being wrong.
    """
    run = run_wrapper(
        tmp_path,
        ["up", "-d", "--build", "--wait"],
        fake_local_addr=DROPLET_ADDR,
    )

    assert run.returncode == 0, str(run)
    assert run.reached_docker, "the guard blocked a deploy from the droplet itself"
    assert DROPLET_ADDR in run.stderr, (
        "an allowed run should say which fact allowed it; the operator has no "
        f"other way to know the detection worked.\n{run}"
    )


def test_deploy_host_from_the_env_file_identifies_a_future_droplet(
    tmp_path: Path,
) -> None:
    """A replacement droplet must not need a code change or a manual marker:
    the DEPLOY_HOST already copied to it in `.env` (docs/deploy.md §4) is
    enough."""
    run = run_wrapper(
        tmp_path,
        ["up", "-d", "--wait"],
        env_lines="DEPLOY_HOST=203.0.113.9\n",
        fake_local_addr="203.0.113.9",
    )

    assert run.returncode == 0, str(run)
    assert run.reached_docker, str(run)
    assert "203.0.113.9" in run.stderr, str(run)


@not_on_the_droplet
@pytest.mark.parametrize("addr", ["127.0.0.1", "192.168.1.50", "10.116.0.4", "172.17.0.1"])
def test_a_loopback_or_private_address_never_passes_as_the_droplet(
    tmp_path: Path, addr: str
) -> None:
    """Every laptop holds a loopback and a LAN address, and the droplet's
    private-network address (10.116.0.4) is not what the repo deploys to. If
    any of these counted, a `DEPLOY_HOST` pointed at a local interface would
    silently disable the guard."""
    run = run_wrapper(
        tmp_path,
        ["up", "-d", "cloudflared"],
        env_lines=f"DEPLOY_HOST={addr}\n",
        fake_local_addr=addr,
    )

    assert run.returncode != 0, str(run)
    assert not run.reached_docker, str(run)


# --------------------------------------------------------------------------
# The override — deliberate, loud, and not persistable
# --------------------------------------------------------------------------


@not_on_the_droplet
def test_the_override_starts_it_and_still_prints_the_warning(tmp_path: Path) -> None:
    run = run_wrapper(
        tmp_path,
        ["up", "-d", "--force-recreate", "cloudflared"],
        shell_env={OVERRIDE_VAR: OVERRIDE_VALUE},
    )

    assert run.returncode == 0, str(run)
    assert run.reached_docker, "the override did not let the invocation through"
    stderr = run.stderr
    assert "WARNING" in stderr, str(run)
    # The override is where the explanation matters most, so it carries the
    # same mechanism the refusal does.
    assert "second connector" in stderr.lower(), str(run)
    assert "does not restore service" in stderr.lower(), str(run)


@not_on_the_droplet
@pytest.mark.parametrize("value", ["1", "true", "yes", "y", "TRUE", "i-know-this", ""])
def test_the_override_must_match_its_exact_value(tmp_path: Path, value: str) -> None:
    """A truthy-looking value is not the override. The point of the exact
    string is that it cannot arrive by accident from a shell profile, a CI
    matrix or a copied command line."""
    run = run_wrapper(
        tmp_path,
        ["up", "-d", "cloudflared"],
        shell_env={OVERRIDE_VAR: value},
    )
    assert run.returncode != 0, str(run)
    assert not run.reached_docker, str(run)


@not_on_the_droplet
def test_the_override_cannot_be_persisted_in_the_env_file(tmp_path: Path) -> None:
    """The wrapper sources `.env` with `set -a`, so a line there would export
    the override into every future invocation on that machine — a guard
    disabled once, silently, forever. It is read from the invoking shell only,
    captured before the file is sourced."""
    run = run_wrapper(
        tmp_path,
        ["up", "-d", "--force-recreate", "cloudflared"],
        env_lines=(
            f"DEPLOY_HOST={DROPLET_ADDR}\n{OVERRIDE_VAR}={OVERRIDE_VALUE}\n"
        ),
    )

    assert run.returncode != 0, (
        "an override set in the env file unlocked the guard; one stale line "
        f"would then disable it permanently.\n{run}"
    )
    assert not run.reached_docker, str(run)
    assert "invoking shell" in run.stderr, str(run)


# --------------------------------------------------------------------------
# Consistency: the guard, the compose file and the docs cannot drift apart
# --------------------------------------------------------------------------


def test_the_guard_derives_the_service_list_from_the_compose_file() -> None:
    """The guard's "was a service named?" test must come from the compose file,
    not from a list maintained in the script — a service added later must not
    read as an unrecognised token.

    Verified by lifting the script's own extraction out of the script and
    running it: if it stops finding the services, the behavioural tests above
    could still pass for the wrong reason (everything refused).
    """
    match = re.search(
        r"compose_services\(\) \{\n  awk '\n(.*?)\n  ' \"\$COMPOSE_FILE\"",
        WRAPPER_TEXT,
        re.DOTALL,
    )
    assert match, "deploy/compose.sh no longer derives its service list with awk"

    extraction = subprocess.run(
        ["awk", match.group(1), str(COMPOSE_FILE)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert extraction.returncode == 0, extraction.stderr
    derived = set(extraction.stdout.split())
    assert derived == {"db", "backend", "redis", "worker", "cloudflared", "portal"}, (
        f"the guard's service extraction returned {sorted(derived)}; it must be "
        "exactly the compose file's services, or a real service name will read "
        "as an unrecognised token (and vice versa)"
    )


def test_the_droplet_address_of_record_matches_the_deploy_doc() -> None:
    """The fallback address is only safe while it is the droplet's. Bound to
    docs/deploy.md, which is where the droplet is recorded."""
    assert DROPLET_ADDR in DEPLOY_DOC.read_text(), (
        f"deploy/compose.sh treats {DROPLET_ADDR} as the droplet, but "
        "docs/deploy.md does not mention it — one of the two is stale, and if "
        "it is the script the guard will refuse a real deploy"
    )


def test_the_cloudflared_readme_records_the_failure_mode_and_the_override() -> None:
    """The guard is only half the fix; someone hitting it has to be able to
    find out why. Both strings are read out of the script, so renaming either
    fails here instead of leaving the doc describing a command that no longer
    works."""
    doc = CLOUDFLARED_DOC.read_text()
    assert OVERRIDE_VAR in doc, (
        "deploy/cloudflared/README.md does not document the override the guard "
        "actually reads"
    )
    assert OVERRIDE_VALUE in doc, doc[:200]
    lowered = doc.lower()
    assert "second connector" in lowered, (
        "deploy/cloudflared/README.md does not record the failure mode the "
        "guard exists to prevent"
    )


def test_the_operational_rule_is_recorded_for_the_owner() -> None:
    """docs/OWNER-ACTIONS.md OA-3 is where the tunnel's operating rules live.
    A guard on one wrapper does not cover a human with an ssh session."""
    oa = OWNER_ACTIONS.read_text()
    lowered = oa.lower()
    assert "second connector" in lowered, (
        "OA-3 does not record that a laptop cloudflared hijacks the live tunnel"
    )
    assert "force-recreate" in lowered and "on the droplet" in lowered, (
        "OA-3 does not record that recovery is a force-recreate ON THE DROPLET"
    )
    assert "/ready" in oa, "OA-3 does not record that /ready is not evidence"


def test_the_build_plan_records_the_stray_connector_hypothesis_without_overstating_it() -> None:
    """§10.6(g) is the historical 64%-failure record. The new outage makes a
    stray second connector a candidate explanation for it — a hypothesis, not
    a proof: nobody enumerated the connectors at the time, and there is still
    no Cloudflare API token to do it with."""
    plan = BUILD_PLAN.read_text()
    lowered = plan.lower()
    assert "second connector" in lowered, (
        "docs/BUILD-PLAN.md does not mention the second-connector mechanism"
    )
    hedged = any(
        term in lowered
        for term in ("hypothesis", "candidate explanation", "not proof", "not proven")
    )
    assert hedged, (
        "docs/BUILD-PLAN.md states the stray-connector explanation for the "
        "historical 64% failure without hedging it; nobody enumerated the "
        "connectors at the time, so it is not established"
    )
