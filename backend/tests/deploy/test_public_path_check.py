"""The public-path stage of ``scripts/verify_deploy.sh``: can the gate fail
when the transport is broken?

WHY THIS EXISTS. Every assertion the script had before 2026-08-17 — the four
liveness checks and ``--deep`` — targets
``${DEPLOY_SCHEME}://${DEPLOY_HOST}:${DEPLOY_PORT}``, the droplet's own
published port. Zendesk cannot reach that address; it reaches the app only
through ``PUBLIC_BASE_URL`` (the Cloudflare hostname the tunnel terminates).
On 2026-08-17 that path returned **502 for ~64% of real deliveries** while the
droplet port answered every request — so the gate would have reported 4/4 and
``--deep`` would have passed through a total outage of the only route the
product actually has (``docs/BUILD-PLAN.md §10.6g``).

``test_a_partial_public_outage_fails_the_gate_and_reports_the_rate`` below
reproduces exactly that split state — droplet healthy, public path
intermittently 502 — and is the test that would have gone red. Everything else
here guards the ways such a stage can be quietly neutralised back into the
false green it replaced: skipping silently, sampling once, or being pointed at
loopback and still reporting the Zendesk path as checked.

SAFETY: identical to the rest of this package (see conftest.py). The real
script is copied into a tmp_path "repo", a fake ``curl`` that never opens a
socket is put on PATH, and ``docker`` is either a hostile canary or a no-op
stub. No test here touches the network, the real ``.env``, or real docker —
and ``PUBLIC_BASE_URL`` is scrubbed from the child environment, so a developer
whose shell has sourced ``.env`` cannot make these tests fire at production.
"""

from __future__ import annotations

import re
from pathlib import Path

from .conftest import (
    run_verify_deploy,
    write_docker_canary,
    write_docker_noop,
    write_fake_curl,
)

DROPLET = "droplet.example.invalid"
PUBLIC = "https://public.example.invalid"

# Small, but never below the script's own floor of 4 — these tests are about
# the stage's behaviour, and each sample is a subprocess.
SAMPLES = "6"

# "n/n = xx.x%" — the rate the stage is required to report instead of a
# boolean. Anchored loosely on purpose: the wording may change, a rate that
# stops being printed at all is the regression.
RATE = re.compile(r"\d+/\d+ = \d+\.\d%")


def _remote_env(**extra: str) -> dict[str, str]:
    env = {
        "DEPLOY_HOST": DROPLET,
        "PORTAL_TOKEN": "dummy-token",
        "CXFORGE_PUBLIC_SAMPLES": SAMPLES,
    }
    env.update(extra)
    return env


def test_the_public_path_is_checked_by_default_in_remote_mode(
    fake_repo: Path, stub_bin: Path
) -> None:
    """No flag required. The stage that closes the blind spot must not itself
    be opt-in: the incident it exists to catch happened while nobody was
    passing extra flags, and ``--deep`` had already demonstrated that an
    opt-in check is a check that is absent by default."""
    sentinel = fake_repo / "docker_canary_triggered.log"
    write_docker_canary(stub_bin, sentinel)
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        env_overrides=_remote_env(PUBLIC_BASE_URL=PUBLIC),
        stub_bin_dir=stub_bin,
        canary_sentinel=sentinel,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert f"PUBLIC PATH: {PUBLIC} (the real hostname, through Cloudflare)" in result.stdout, (
        combined
    )
    # Both probes, both reported as rates, both green.
    assert f"GET  /health           -> {SAMPLES}/{SAMPLES} = 100.0%" in result.stdout, combined
    assert f"POST /webhooks/zendesk -> {SAMPLES}/{SAMPLES} = 100.0%" in result.stdout, combined
    assert "PUBLIC PATH: PASS" in result.stdout, combined
    assert "SCOPE: PUBLIC PATH" in result.stdout, combined
    assert "CHECKED, all green" in result.stdout, combined
    assert not sentinel.exists(), sentinel.read_text()


def test_a_partial_public_outage_fails_the_gate_and_reports_the_rate(
    fake_repo: Path, stub_bin: Path
) -> None:
    """THE test. The droplet port is healthy; the public hostname 502s for
    two of every three requests. That is the 2026-08-17 state, and the run
    must go red with the RATE in the message — not pass, and not fail on a
    boolean that hides how bad it is.

    The fault is injected on the PUBLIC hostname only, which is what makes
    this test also prove the stage sends its requests there: if it were
    probing ``$base`` (the droplet) like every other assertion in the script,
    nothing would 502 and this test would pass green.
    """
    sentinel = fake_repo / "docker_canary_triggered.log"
    write_docker_canary(stub_bin, sentinel)
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        env_overrides=_remote_env(
            PUBLIC_BASE_URL=PUBLIC,
            FAKE_CURL_FLAKY_MATCH="public.example.invalid",
            FAKE_CURL_FLAKY_PATTERN="100",  # 1 served, 2 x 502 -> 33% success
            FAKE_CURL_FLAKY_STATE=str(fake_repo / "flaky.counter"),
        ),
        stub_bin_dir=stub_bin,
        canary_sentinel=sentinel,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "FAIL:" in result.stderr, combined
    assert "the PUBLIC path is not serving reliably" in result.stderr, combined
    # A rate, in the failure message itself.
    assert RATE.search(result.stderr), result.stderr
    # And the histogram, so the reader can tell a 502 (edge reached, origin
    # dead) from a 000 (never reached Cloudflare at all).
    assert "502 x" in result.stderr, result.stderr

    # The liveness assertions all passed against the droplet port in this very
    # same run — the exact split the old gate could not see.
    assert "4/4: GET /api/metrics with X-Portal-Token -> 200" in result.stdout, combined
    # And no PASS of any kind survived it.
    assert "PASS" not in result.stdout, combined
    assert not sentinel.exists(), sentinel.read_text()


def test_an_unset_public_base_url_skips_loudly_and_never_reads_as_checked(
    fake_repo: Path, stub_bin: Path
) -> None:
    """Degrading honestly. With no hostname to check, the run may still pass —
    but it has to say, in the run output AND on the SCOPE: line, that the only
    route Zendesk has was not exercised. A silent pass here would rebuild the
    original defect one level up."""
    sentinel = fake_repo / "docker_canary_triggered.log"
    write_docker_canary(stub_bin, sentinel)
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        env_overrides=_remote_env(),
        stub_bin_dir=stub_bin,
        canary_sentinel=sentinel,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "PUBLIC PATH: NOT CHECKED" in result.stdout, combined
    scope = [ln for ln in result.stdout.splitlines() if "SCOPE: PUBLIC PATH" in ln]
    assert len(scope) == 1, combined
    assert "NOT CHECKED" in scope[0], scope
    # It must not be possible to read this run as having checked the path.
    assert "PUBLIC PATH: PASS" not in result.stdout, combined
    assert "CHECKED, all green" not in result.stdout, combined
    assert not sentinel.exists(), sentinel.read_text()


def test_the_public_flag_turns_an_absent_hostname_into_a_hard_failure(
    fake_repo: Path, stub_bin: Path
) -> None:
    """``--public`` is for callers that need the public path checked or
    nothing: a skip becomes a hard failure, before a single request goes out —
    the same discipline ``--deep`` applies to a missing signing secret."""
    sentinel = fake_repo / "docker_canary_triggered.log"
    write_docker_canary(stub_bin, sentinel)
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        args=["--public"],
        env_overrides=_remote_env(),
        stub_bin_dir=stub_bin,
        canary_sentinel=sentinel,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "--public was passed but PUBLIC_BASE_URL is empty" in result.stderr, combined
    assert "PASS" not in result.stdout, combined


def test_the_sample_count_cannot_be_set_below_the_floor(
    fake_repo: Path, stub_bin: Path
) -> None:
    """Anti-tamper. The stage's whole value is that it samples: at the measured
    64% failure rate a single request misses the outage 36% of the time. So
    ``CXFORGE_PUBLIC_SAMPLES=1`` — the obvious way to make a red gate green
    without fixing anything — is refused, and so is a value that would parse
    as 0 and send no requests at all."""
    write_fake_curl(stub_bin)

    for value in ("1", "0", "twenty", ""):
        result = run_verify_deploy(
            fake_repo,
            env_overrides={
                "DEPLOY_HOST": DROPLET,
                "PORTAL_TOKEN": "dummy-token",
                "PUBLIC_BASE_URL": PUBLIC,
                "CXFORGE_PUBLIC_SAMPLES": value,
            },
            stub_bin_dir=stub_bin,
        )
        combined = result.stdout + result.stderr
        if value == "":
            # An empty value is indistinguishable from "not set" in shell
            # parameter expansion, so it falls back to the default of 20 and
            # the run is a normal pass. Asserted rather than skipped so the
            # fallback is a documented behaviour and not an accident.
            assert result.returncode == 0, combined
            assert "20/20 = 100.0%" in result.stdout, combined
            continue
        assert result.returncode != 0, f"CXFORGE_PUBLIC_SAMPLES={value!r}: {combined}"
        assert "CXFORGE_PUBLIC_SAMPLES" in result.stderr, combined
        assert "PASS" not in result.stdout, combined


def test_a_loopback_hostname_is_labelled_simulated_and_not_reported_as_the_real_path(
    fake_repo: Path, stub_bin: Path
) -> None:
    """The other way to fake a green: point PUBLIC_BASE_URL at something local
    that always answers. Allowed — it is how the stage is proven able to fail —
    but it must never be reportable as evidence about Zendesk's route, so the
    label rides on the pass line and the SCOPE: line."""
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        env_overrides=_remote_env(PUBLIC_BASE_URL="http://127.0.0.1:9911"),
        stub_bin_dir=stub_bin,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "SIMULATED — loopback, NOT the real transport" in result.stdout, combined
    scope = [ln for ln in result.stdout.splitlines() if "SCOPE: PUBLIC PATH" in ln]
    assert len(scope) == 1, combined
    assert "SIMULATED" in scope[0], scope


def test_an_exported_public_base_url_survives_sourcing_a_dotenv_that_clobbers_it(
    fake_repo: Path, stub_bin: Path
) -> None:
    """The T-17 clobber, applied to the new variable. ``.env.example`` ships
    ``PUBLIC_BASE_URL=`` as a bare empty assignment, and the script does
    ``set -a; source .env`` — so without the same capture/restore
    ``DEPLOY_HOST`` needed, an exported hostname would be overwritten with ""
    and this stage would silently SKIP. That is the original bug reproduced
    against the check that exists because the gate was blind."""
    (fake_repo / ".env").write_text("PUBLIC_BASE_URL=\nDEPLOY_HOST=\n")
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        env_overrides=_remote_env(PUBLIC_BASE_URL=PUBLIC),
        stub_bin_dir=stub_bin,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert f"PUBLIC PATH: {PUBLIC}" in result.stdout, combined
    assert "PUBLIC PATH: NOT CHECKED" not in result.stdout, combined


def test_local_mode_skips_the_public_path_unless_it_is_asked_for(
    fake_repo: Path, stub_bin: Path
) -> None:
    """A ``--local`` run verifies the stack on this machine. The public
    hostname fronts the DROPLET, so checking it there would report on a
    deployment this run never touched — the same category error as a local
    pass reading as droplet evidence, which is what ``--local`` was made
    explicit to prevent. ``--public`` overrides it."""
    write_docker_noop(stub_bin)
    write_fake_curl(stub_bin)

    skipped = run_verify_deploy(
        fake_repo,
        args=["--local"],
        env_overrides={
            "PORTAL_TOKEN": "dummy-token",
            "PUBLIC_BASE_URL": PUBLIC,
            "CXFORGE_PUBLIC_SAMPLES": SAMPLES,
        },
        stub_bin_dir=stub_bin,
    )
    combined = skipped.stdout + skipped.stderr
    assert skipped.returncode == 0, combined
    assert "PUBLIC PATH: NOT CHECKED — LOCAL mode" in skipped.stdout, combined

    forced = run_verify_deploy(
        fake_repo,
        args=["--local", "--public"],
        env_overrides={
            "PORTAL_TOKEN": "dummy-token",
            "PUBLIC_BASE_URL": PUBLIC,
            "CXFORGE_PUBLIC_SAMPLES": SAMPLES,
        },
        stub_bin_dir=stub_bin,
    )
    combined = forced.stdout + forced.stderr
    assert forced.returncode == 0, combined
    assert "PUBLIC PATH: PASS" in forced.stdout, combined


def test_the_stage_really_requests_the_zendesk_endpoint_on_the_public_host(
    fake_repo: Path, stub_bin: Path
) -> None:
    """Bound on the REQUESTS, not on the script's own output.

    This test exists because of a sabotage that nothing else here caught:
    replacing the ``POST /webhooks/zendesk`` probe with a second
    ``GET /health`` left all the other assertions green, since the stage
    composes its own report line. So the fake curl journals every request and
    this asserts the traffic itself — the right count, the right method, the
    right path, and on the PUBLIC host rather than the droplet port.

    The count is load-bearing twice over: it is one request per sample (the
    stage must not collapse N samples into one connection, which is how the
    probabilistic outage stayed invisible), and it is the Zendesk endpoint
    that carries them.
    """
    journal = fake_repo / "curl.journal"
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        env_overrides=_remote_env(PUBLIC_BASE_URL=PUBLIC, FAKE_CURL_JOURNAL=str(journal)),
        stub_bin_dir=stub_bin,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined

    requests = journal.read_text().splitlines()
    n = int(SAMPLES)
    assert requests.count(f"POST {PUBLIC}/webhooks/zendesk") == n, requests
    assert requests.count(f"GET {PUBLIC}/health") == n, requests
    # Nothing in the public stage may quietly fall back to the droplet port:
    # the only droplet requests in the journal are the four liveness ones.
    droplet = [line for line in requests if DROPLET in line]
    assert len(droplet) == 5, droplet  # 4 assertions + the index-body read
    assert not any(f"{DROPLET}:8080/webhooks/zendesk" in line for line in requests), requests


def test_the_scope_block_distinguishes_the_path_it_asserted_from_the_public_one(
    fake_repo: Path, stub_bin: Path
) -> None:
    """The point of the whole change: a reader must not be able to mistake a
    droplet-port pass for a Zendesk-path pass. So the SCOPE: block names the
    address that was actually asserted against, separately from the public
    path's own result."""
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        env_overrides=_remote_env(PUBLIC_BASE_URL=PUBLIC),
        stub_bin_dir=stub_bin,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    asserted = [ln for ln in result.stdout.splitlines() if "SCOPE: PATH ASSERTED" in ln]
    assert len(asserted) == 1, combined
    assert f"http://{DROPLET}:8080" in asserted[0], asserted
    assert "Zendesk cannot reach that address" in asserted[0], asserted
    public = [ln for ln in result.stdout.splitlines() if "SCOPE: PUBLIC PATH" in ln]
    assert len(public) == 1, combined
    assert PUBLIC in public[0], public
    assert RATE.search(public[0]), public
