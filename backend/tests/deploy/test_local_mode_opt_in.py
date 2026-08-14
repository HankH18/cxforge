"""T-17 acceptance 2/3 — LOCAL mode is opt-in only, and its success output
can never be mistaken for droplet evidence.

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
"""

from __future__ import annotations

from pathlib import Path

from .conftest import (
    run_verify_deploy,
    write_docker_canary,
    write_docker_noop,
    write_fake_curl,
)


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
