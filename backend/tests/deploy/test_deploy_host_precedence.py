"""T-17 acceptance 1 — regression test for the exported-DEPLOY_HOST clobber.

Before the fix, ``scripts/verify_deploy.sh`` did
``set -a; source .env; set +a``, and ``.env.example`` (and any real
``.env`` freshly copied from it) defines ``DEPLOY_HOST=`` as a bare EMPTY
assignment. Because ``set -a`` auto-exports every assignment ``.env``
makes in the CURRENT shell, sourcing it silently overwrote an
already-exported ``DEPLOY_HOST`` with the empty string — the script then
fell through to LOCAL mode while still printing an unqualified ``PASS``.
That is exactly the bug T-17 exists to fix.

This test reproduces the clobber shape with a FAKE ``.env`` (never the
real one, which is out of scope and holds secrets) and asserts the fixed
script takes the REMOTE branch anyway. See conftest.py's module docstring
for the full safety rationale — no real docker, no real network call,
anywhere in this file.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import (
    run_verify_deploy,
    write_docker_canary,
    write_fake_curl,
)


def test_exported_deploy_host_survives_sourcing_an_env_file_that_clobbers_it(
    fake_repo: Path, stub_bin: Path
) -> None:
    # Reproduce the exact clobber shape: .env defines DEPLOY_HOST as a
    # bare empty assignment — precisely what .env.example ships, and what
    # a real .env copied from it looks like before a human fills it in.
    (fake_repo / ".env").write_text("DEPLOY_HOST=\n")

    sentinel = fake_repo / "docker_canary_triggered.log"
    write_docker_canary(stub_bin, sentinel)
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        env_overrides={
            "DEPLOY_HOST": "example-remote-host.invalid",
            "PORTAL_TOKEN": "dummy-token",
        },
        stub_bin_dir=stub_bin,
        canary_sentinel=sentinel,
    )

    combined = result.stdout + result.stderr

    # verify_remote()'s very first statement, printed before any curl call
    # — its presence alone proves the exported DEPLOY_HOST was NOT
    # clobbered by sourcing .env's empty DEPLOY_HOST= line.
    assert (
        "DEPLOY_HOST=example-remote-host.invalid -> verifying the REMOTE deploy"
        in result.stdout
    ), combined

    # Belt-and-suspenders: the LOCAL/docker branch was never entered.
    assert not sentinel.exists(), (
        f"docker canary was triggered — LOCAL branch ran:\n{sentinel.read_text()}"
    )

    # With the fake curl answering, the whole run goes green, mode-qualified.
    assert result.returncode == 0, combined
    assert (
        "PASS (REMOTE: verified droplet at example-remote-host.invalid)" in result.stdout
    ), combined
    # The old, mode-blind bare "PASS" line must never appear again.
    assert "[verify_deploy] PASS\n" not in result.stdout


def test_env_defined_deploy_host_still_applies_when_nothing_was_exported(
    fake_repo: Path, stub_bin: Path
) -> None:
    """Companion sanity check: precedence favors an export over .env, it
    doesn't ignore .env outright — when the caller genuinely never
    exported DEPLOY_HOST, .env's own value must still take effect."""
    (fake_repo / ".env").write_text(
        "DEPLOY_HOST=example-env-host.invalid\nPORTAL_TOKEN=dummy-token\n"
    )
    sentinel = fake_repo / "docker_canary_triggered.log"
    write_docker_canary(stub_bin, sentinel)
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        stub_bin_dir=stub_bin,
        canary_sentinel=sentinel,
    )

    combined = result.stdout + result.stderr
    assert (
        "DEPLOY_HOST=example-env-host.invalid -> verifying the REMOTE deploy"
        in result.stdout
    ), combined
    assert not sentinel.exists()
    assert result.returncode == 0, combined
