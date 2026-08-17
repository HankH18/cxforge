"""T-17 acceptance 2/3 — LOCAL mode is opt-in only, and its success output
can never be mistaken for droplet evidence.

Plus (2026-08-17) the rule that LOCAL mode must never start ``cloudflared``.
See ``test_local_mode_never_issues_a_compose_up_that_can_start_the_tunnel``.

SAFETY: none of these tests execute real docker.
  * ``test_no_flag_and_empty_deploy_host_is_a_hard_failure`` never even
    reaches the docker checks — the fixed script now hard-fails before
    ever calling ``command -v docker`` / ``docker info`` — and is guarded
    by the same hostile canary as the precedence tests, so a future
    regression that made it reach docker anyway would be caught.
  * ``test_local_flag_with_empty_deploy_host_runs_local_...`` exercises
    the LOCAL success path entirely through a no-op stub ``docker`` (see
    conftest.write_docker_noop) that just returns success for `info` and
    `compose ... up/down` — no real container is ever started.
  * ``test_local_flag_is_ignored_when_deploy_host_is_exported`` proves
    ``--local`` can't override an exported ``DEPLOY_HOST``, guarded again
    by the hostile docker canary.
  * the tunnel tests use the journaling stub ``docker`` (write_docker_journal),
    which records its argv and exits 0. Nothing is ever started — least of all
    a ``cloudflared``, which is the whole point.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .conftest import (
    REPO_ROOT,
    run_verify_deploy,
    write_docker_canary,
    write_docker_journal,
    write_docker_noop,
    write_fake_curl,
)

DEPLOY_COMPOSE = REPO_ROOT / "deploy" / "docker-compose.yml"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_deploy.sh"

# Any compose subcommand that can bring a container up. `up` is the one
# verify_local uses; the rest are listed so a future rewrite that reached for
# `start` or `create` instead is held to the same rule rather than sliding
# past a test that only knew one verb.
COMPOSE_START_VERBS = ("up", "start", "restart", "create", "run", "scale")

# Any compose subcommand the cleanup trap could plausibly be written with.
# `down` is the one it uses; `stop`/`rm`/`kill` are here so a rewrite that
# reached for those (e.g. if `down` had not accepted a service list) is held to
# the same rule.
COMPOSE_TEARDOWN_VERBS = ("down", "stop", "rm", "kill")


def _removes_volumes(token: str) -> bool:
    """Does this argv token ask docker to DELETE volumes?

    ``-v`` / ``--volumes`` on ``down`` removes the named volumes declared in the
    compose file's ``volumes:`` section — for project ``othram-deploy`` that is
    ``othram-deploy-pgdata``, which on the droplet is the production Postgres.
    Long flags are matched exactly so ``--renew-anon-volumes`` (an ``up`` flag
    that recreates anonymous volumes and destroys no named one) is not caught;
    short flags are matched inside a cluster so ``-vf`` cannot slip past.
    """
    if token in ("-v", "--volumes"):
        return True
    return (
        token.startswith("-") and not token.startswith("--") and "v" in token[1:]
    )


def _compose_services() -> set[str]:
    loaded = yaml.safe_load(DEPLOY_COMPOSE.read_text())
    return set((loaded.get("services") or {}).keys())


def _declared_local_services() -> list[str]:
    """The literal service list ``verify_deploy.sh`` pins for LOCAL mode."""
    match = re.search(
        r"^LOCAL_SERVICES=\(([^)]*)\)", VERIFY_SCRIPT.read_text(), re.MULTILINE
    )
    assert match, "scripts/verify_deploy.sh no longer defines LOCAL_SERVICES=(...)"
    return match.group(1).split()


def _journal_lines(journal: Path) -> list[list[str]]:
    assert journal.exists(), (
        "the journaling docker stub was never invoked — the run under test did "
        "not reach docker at all, so this test proves nothing about its argv"
    )
    return [line.split() for line in journal.read_text().splitlines() if line.strip()]


def test_no_flag_and_empty_deploy_host_is_a_hard_failure(
    fake_repo: Path, stub_bin: Path
) -> None:
    sentinel = fake_repo / "docker_canary_triggered.log"
    write_docker_canary(stub_bin, sentinel)

    result = run_verify_deploy(
        fake_repo,
        env_overrides={"PORTAL_TOKEN": "dummy-token"},
        stub_bin_dir=stub_bin,
        canary_sentinel=sentinel,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "FAIL:" in result.stderr, combined
    assert "--local was not passed" in result.stderr, combined
    # No PASS of any kind, anywhere in stdout.
    assert "PASS" not in result.stdout, combined
    assert not sentinel.exists(), (
        f"docker canary was triggered — LOCAL branch ran:\n{sentinel.read_text()}"
    )


def test_local_flag_with_empty_deploy_host_runs_local_and_never_prints_bare_pass(
    fake_repo: Path, stub_bin: Path
) -> None:
    write_docker_noop(stub_bin)
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        args=["--local"],
        env_overrides={"PORTAL_TOKEN": "dummy-token"},
        stub_bin_dir=stub_bin,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "LOCAL-MODE PASS" in result.stdout, combined
    assert "NOT a droplet" in result.stdout, combined
    # The old, mode-blind bare "PASS" line must never appear on its own —
    # LOCAL success must be textually distinguishable from droplet evidence.
    stdout_lines = result.stdout.splitlines()
    assert "[verify_deploy] PASS" not in stdout_lines, combined


def test_local_flag_is_ignored_when_deploy_host_is_exported(
    fake_repo: Path, stub_bin: Path
) -> None:
    sentinel = fake_repo / "docker_canary_triggered.log"
    write_docker_canary(stub_bin, sentinel)
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        args=["--local"],
        env_overrides={
            "DEPLOY_HOST": "example-remote-host.invalid",
            "PORTAL_TOKEN": "dummy-token",
        },
        stub_bin_dir=stub_bin,
        canary_sentinel=sentinel,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert (
        "PASS (REMOTE: verified droplet at example-remote-host.invalid)" in result.stdout
    ), combined
    assert "LOCAL-MODE" not in result.stdout, combined
    assert not sentinel.exists(), (
        f"docker canary was triggered — LOCAL branch ran despite exported DEPLOY_HOST:\n"
        f"{sentinel.read_text()}"
    )


# --------------------------------------------------------------------------
# LOCAL mode must never start the tunnel (2026-08-17)
# --------------------------------------------------------------------------


def test_local_mode_never_issues_a_compose_up_that_can_start_the_tunnel(
    fake_repo: Path, stub_bin: Path
) -> None:
    """The hazard: ``verify_local`` used to run

        docker compose -f deploy/docker-compose.yml up -d --build --wait

    with NO service list, after ``set -a; source .env``. A bare ``up`` starts
    EVERY service in the file — ``cloudflared`` included. ``CLOUDFLARE_TUNNEL_TOKEN``
    identifies the *tunnel*, not the host, so that connector joins the LIVE
    tunnel and Cloudflare's edge can route real public traffic into this
    machine's throwaway stack; then the cleanup trap's ``down -v`` removes it
    and the edge holds stale routes. Measured 2026-08-17: 10/10 public 502,
    and ``docker stop`` did NOT restore service — only a force-recreate on the
    droplet did (deploy/compose.sh TRAP 2, docs/BUILD-PLAN.md §10.6g).

    It was inert on the owner's machine only by accident: that ``.env`` sets
    ``DEPLOY_HOST``, which makes ``--local`` a no-op. On a clone with the
    ``.env.example`` default (empty ``DEPLOY_HOST``) and a populated token, it
    reproduces the outage from the script whose job is to verify a deploy.

    Bound on the ARGV, not on the script's output: verify_deploy.sh composes
    its own progress lines, so a bare ``up`` and a correctly scoped one print
    the same thing. Nothing is started here — the stub ``docker`` only writes
    its argv to a file.
    """
    journal = fake_repo / "docker.journal"
    write_docker_journal(stub_bin, journal)
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        args=["--local"],
        env_overrides={
            "PORTAL_TOKEN": "dummy-token",
            "DOCKER_JOURNAL": str(journal),
        },
        stub_bin_dir=stub_bin,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined

    invocations = _journal_lines(journal)
    services = _compose_services()
    assert "cloudflared" in services, (
        "precondition: deploy/docker-compose.yml is expected to define a "
        "cloudflared service — this test is about not starting it"
    )
    expected = services - {"cloudflared"}

    # 1. cloudflared is never named, in ANY docker invocation this run makes.
    for argv in invocations:
        assert not any("cloudflared" in token for token in argv), (
            "LOCAL mode named cloudflared in a docker invocation: "
            f"{' '.join(argv)}"
        )

    # 2. Every start verb carries an explicit service list, and that list is
    #    exactly the application services. An empty list is the bare `up` —
    #    which names nothing and therefore starts everything, cloudflared
    #    included — so "no services named" must fail here, not pass.
    starts = [
        argv
        for argv in invocations
        if any(token in COMPOSE_START_VERBS for token in argv)
    ]
    assert starts, (
        "LOCAL mode issued no container-starting docker compose command at "
        f"all; journal was:\n{journal.read_text()}"
    )
    for argv in starts:
        verb_at = next(
            i for i, token in enumerate(argv) if token in COMPOSE_START_VERBS
        )
        named = {token for token in argv[verb_at + 1 :] if token in services}
        assert named == expected, (
            f"`docker {' '.join(argv)}` names services {sorted(named)}; "
            f"expected exactly {sorted(expected)}. A compose start verb with no "
            "service list starts EVERY service in deploy/docker-compose.yml, "
            "including cloudflared — that is the 2026-08-17 public outage, run "
            "from the deploy verifier."
        )


def test_local_mode_teardown_can_never_remove_a_volume_it_did_not_create(
    fake_repo: Path, stub_bin: Path
) -> None:
    """The hazard: ``verify_local``'s cleanup trap used to run

        docker compose -f deploy/docker-compose.yml down -v --remove-orphans

    with no service list. Compose project ``othram-deploy`` is production ON THE
    DROPLET, and ``-v`` removes that project's NAMED volumes — including
    ``othram-deploy-pgdata``, the production Postgres holding ``runs``,
    ``drafts``, ``tickets_seen``, the seeded ``cases`` and the kb_chunks.

    Reachable exactly the way the sibling ``up`` hazard was: a droplet whose
    ``.env`` leaves ``DEPLOY_HOST`` empty (the ``.env.example`` default) plus a
    human typing ``--local``. It is strictly worse than that one. Starting the
    wrong services causes an outage recoverable by an ``up``; deleting
    ``pgdata`` is not recoverable at all.

    Scoping ``-v`` to a service list does NOT fix it, which is why this test
    forbids the flag rather than the flag-without-services. Measured
    2026-08-17 on a throwaway two-service compose project: ``down -v alpha``
    removed alpha's named volume and left beta's — so ``-v`` does honour a
    service list, and it does not matter, because ``db`` is in
    ``LOCAL_SERVICES`` and ``deploy_pgdata`` is ``db``'s volume.

    Bound on the ARGV, like its sibling above: the script composes its own
    teardown line, so a safe teardown and a destructive one print the same
    thing. Nothing is executed — the stub ``docker`` writes its argv and exits
    0, so no volume on this machine is ever at risk from this test either.
    """
    journal = fake_repo / "docker.journal"
    write_docker_journal(stub_bin, journal)
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        args=["--local"],
        env_overrides={
            "PORTAL_TOKEN": "dummy-token",
            "DOCKER_JOURNAL": str(journal),
        },
        stub_bin_dir=stub_bin,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined

    invocations = _journal_lines(journal)
    services = _compose_services()
    expected = set(_declared_local_services())
    assert expected, "precondition: LOCAL_SERVICES must not be empty"

    # 1. The trap actually fired. Without this the rest of the test would pass
    #    vacuously against a script that never tore anything down.
    teardowns = [
        argv
        for argv in invocations
        if any(token in COMPOSE_TEARDOWN_VERBS for token in argv)
    ]
    assert teardowns, (
        "LOCAL mode issued no teardown docker compose command at all — the "
        "cleanup trap did not run, so this test proves nothing about what it "
        f"would have removed. Journal was:\n{journal.read_text()}"
    )

    # 2. No docker invocation ANYWHERE in the run asks for volume removal.
    for argv in invocations:
        offending = [token for token in argv if _removes_volumes(token)]
        assert not offending, (
            f"`docker {' '.join(argv)}` passes {offending}, which removes the "
            "othram-deploy project's NAMED volumes. On the droplet that "
            "deletes othram-deploy-pgdata — the production Postgres. There is "
            "no recovery from that, so LOCAL mode must never pass it, with or "
            "without a service list (a service list does not help: `db` is in "
            "LOCAL_SERVICES and deploy_pgdata is db's volume)."
        )

    # 3. Every teardown names exactly the services LOCAL mode started, read out
    #    of the script's own LOCAL_SERVICES array so the two lists cannot drift.
    #    A teardown that names nothing acts on the whole project.
    for argv in teardowns:
        verb_at = next(
            i for i, token in enumerate(argv) if token in COMPOSE_TEARDOWN_VERBS
        )
        named = {token for token in argv[verb_at + 1 :] if token in services}
        assert named == expected, (
            f"`docker {' '.join(argv)}` tears down services {sorted(named)}; "
            f"expected exactly {sorted(expected)} (the script's own "
            "LOCAL_SERVICES). A compose teardown verb with no service list "
            "acts on EVERY container in project othram-deploy — which on the "
            "droplet is production, cloudflared included."
        )

        # 4. --remove-orphans acts on the project, not on the named services:
        #    it removes any othram-deploy container whose service is absent from
        #    THIS checkout's compose file. LOCAL mode starts only services that
        #    are in this file, so it can never create an orphan — the flag could
        #    only ever remove a container this run did not create.
        assert "--remove-orphans" not in argv, (
            f"`docker {' '.join(argv)}` passes --remove-orphans. LOCAL mode "
            "cannot create an orphan, so this can only remove a container it "
            "did not create — on a droplet whose checkout has drifted from the "
            "deployed compose file, that is a production container."
        )

    # 5. And it must not delete volumes by hand either — a `docker volume rm`
    #    would be the same data loss with a different spelling.
    for argv in invocations:
        assert argv[:1] != ["volume"], (
            f"`docker {' '.join(argv)}` manipulates volumes directly; LOCAL "
            "mode must not remove volumes at all"
        )


def test_the_local_service_list_is_every_service_except_the_tunnel() -> None:
    """Drift guard for the literal above.

    ``LOCAL_SERVICES`` is spelled out in the script so a reader can see that
    ``cloudflared`` is absent, which means a service added to
    deploy/docker-compose.yml later would silently stop being verified by
    ``--local``. That drift fails here rather than going unnoticed.
    """
    declared = _declared_local_services()
    services = _compose_services()
    assert "cloudflared" not in declared, (
        f"scripts/verify_deploy.sh's LOCAL_SERVICES includes cloudflared: {declared}"
    )
    assert set(declared) == services - {"cloudflared"}, (
        f"LOCAL_SERVICES={declared} does not match deploy/docker-compose.yml's "
        f"services minus cloudflared ({sorted(services - {'cloudflared'})}). Either "
        "a service was added to the compose file and --local no longer verifies "
        "it, or one was removed and --local now names a service that does not "
        "exist (which makes `docker compose up` fail outright)."
    )
    # No duplicates: a duplicated name would satisfy the set comparison above
    # while making the invocation itself odd to read.
    assert len(declared) == len(set(declared)), declared


def test_local_mode_says_out_loud_that_it_did_not_check_the_public_path(
    fake_repo: Path, stub_bin: Path
) -> None:
    """A LOCAL run that silently omits the tunnel is fine; one that leaves a
    reader thinking the public path was checked is not.

    The script has a public-path stage that samples ``PUBLIC_BASE_URL`` and a
    ``SCOPE:`` block that states which paths were exercised. Since LOCAL mode
    starts no cloudflared, the SCOPE block must say so — both when
    ``PUBLIC_BASE_URL`` is set (skipped: it fronts the droplet) and when it is
    empty (skipped: nothing to check).
    """
    journal = fake_repo / "docker.journal"
    write_docker_journal(stub_bin, journal)
    write_fake_curl(stub_bin)

    base_env = {"PORTAL_TOKEN": "dummy-token", "DOCKER_JOURNAL": str(journal)}

    for label, overrides in (
        ("PUBLIC_BASE_URL set", {"PUBLIC_BASE_URL": "https://public.invalid"}),
        ("PUBLIC_BASE_URL empty", {}),
    ):
        result = run_verify_deploy(
            fake_repo,
            args=["--local"],
            env_overrides={**base_env, **overrides},
            stub_bin_dir=stub_bin,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"{label}: {combined}"
        assert "PUBLIC PATH: NOT CHECKED" in result.stdout, f"{label}: {combined}"

        scope = [ln for ln in result.stdout.splitlines() if "SCOPE: PUBLIC PATH" in ln]
        assert len(scope) == 1, f"{label}: {combined}"
        assert "did NOT start cloudflared" in scope[0], (
            f"{label}: the SCOPE: PUBLIC PATH line does not say the tunnel was "
            f"never started, so a reader cannot tell this run said nothing about "
            f"the public route:\n{scope[0]}"
        )
