"""W3-G2 — the deep deploy check, and the ways it must refuse to lie.

Two halves.

*Script half* (built on this package's existing fixtures — a copy of the real
``scripts/verify_deploy.sh`` in a disposable repo, a fake ``curl`` that never
opens a socket, and the hostile ``docker`` canary): the ``--deep`` flag's
preconditions are hard failures checked before a single request goes out, and
a run WITHOUT ``--deep`` says out loud that the core loop was not exercised.
That second one is the point of the whole package. ``docs/STATE.md §6.2``
records that this script's unqualified PASS is what carried "the deploy
works" for weeks across a stack with a dead core loop; a pass whose scope is
not stated is how that happens again.

*Logic half*: the decision rules in ``scripts/verify_core_loop.py`` that
decide whether a deployment passed. They are pure functions precisely so
they can be tested for the false-pass cases that are otherwise only
reachable by breaking a live deployment — most importantly that a
``runs`` row left behind by an EARLIER invocation can never be accepted as
evidence that this one worked.

SAFETY: no test here executes real docker, opens a socket, or reaches the
network. See conftest.py's module docstring.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ingress.models import ZendeskWebhookPayload
from ingress.signature import SignatureVerificationError, verify_signature
from scripts.verify_core_loop import (
    DeepCheckFailed,
    assert_row_is_real,
    build_signed_request,
    cleanup_statements,
    find_new_run,
)

from .conftest import (
    run_verify_deploy,
    write_docker_canary,
    write_fake_curl,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_deploy.sh"
DEPLOY_DOC = REPO_ROOT / "docs" / "deploy.md"

SECRET = "a-signing-secret-that-is-not-valid-base64"


# ---------------------------------------------------------------------------
# Script half
# ---------------------------------------------------------------------------


def test_a_pass_without_deep_states_that_the_core_loop_was_not_exercised(
    fake_repo: Path, stub_bin: Path
) -> None:
    """The four liveness assertions passing must never read as "it works".

    This is the assertion the whole work package exists for: the same four
    green ticks were true of a droplet whose webhook accepted events and
    never started a run.
    """
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
    assert result.returncode == 0, combined
    assert "SCOPE: liveness only" in result.stdout, combined
    assert "core loop" in result.stdout, combined
    assert "was NOT exercised" in result.stdout, combined
    assert "--deep" in result.stdout, combined
    assert not sentinel.exists()


def test_deep_with_no_signing_secret_hard_fails_before_any_request(
    fake_repo: Path, stub_bin: Path
) -> None:
    """A missing precondition is a failure, never a silently skipped check.

    Also pins the ORDERING: the failure must land before the first assertion
    prints, so a caller can never watch four green ticks scroll past and
    then discover the check that matters could not run.
    """
    sentinel = fake_repo / "docker_canary_triggered.log"
    write_docker_canary(stub_bin, sentinel)
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        args=["--deep"],
        env_overrides={
            "DEPLOY_HOST": "example-remote-host.invalid",
            "PORTAL_TOKEN": "dummy-token",
            "ZENDESK_WEBHOOK_SIGNING_SECRET": "",
            "CXFORGE_VERIFY_TICKET_ID": "12345",
        },
        stub_bin_dir=stub_bin,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "FAIL:" in result.stderr, combined
    assert "ZENDESK_WEBHOOK_SIGNING_SECRET" in result.stderr, combined
    assert "PASS" not in result.stdout, combined
    assert "1/4" not in result.stdout, combined


def test_deep_with_no_ticket_id_hard_fails_and_says_why_one_cannot_be_invented(
    fake_repo: Path, stub_bin: Path
) -> None:
    write_fake_curl(stub_bin)

    result = run_verify_deploy(
        fake_repo,
        args=["--deep"],
        env_overrides={
            "DEPLOY_HOST": "example-remote-host.invalid",
            "PORTAL_TOKEN": "dummy-token",
            "ZENDESK_WEBHOOK_SIGNING_SECRET": SECRET,
            "CXFORGE_VERIFY_TICKET_ID": "",
        },
        stub_bin_dir=stub_bin,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "CXFORGE_VERIFY_TICKET_ID" in result.stderr, combined
    # The reason matters as much as the requirement: a reader who does not
    # know why has every incentive to "fix" this by generating an id.
    assert "fetch_ticket" in result.stderr, combined
    assert "PASS" not in result.stdout, combined


def test_deploy_doc_documents_the_deep_flag_the_script_actually_parses() -> None:
    """Mirrors the ``--local`` check in test_docs_and_env_example_consistency.

    The flag is read out of the script rather than hardcoded, so renaming it
    without updating the doc fails here instead of leaving the doc describing
    an invocation that no longer works.
    """
    assert "--deep)" in VERIFY_SCRIPT.read_text(), "verify_deploy.sh no longer parses --deep"
    doc = DEPLOY_DOC.read_text()
    assert re.search(r"--deep\b", doc), "docs/deploy.md does not mention the --deep flag"
    assert "CXFORGE_VERIFY_TICKET_ID" in doc, (
        "docs/deploy.md does not tell the operator about the one variable --deep "
        "cannot run without"
    )


# ---------------------------------------------------------------------------
# Logic half — signing
# ---------------------------------------------------------------------------


def test_the_bytes_that_get_posted_are_the_bytes_that_get_signed() -> None:
    """Verified with the SERVER's own verifier, against the exact buffer.

    `ingress/signature.py`'s docstring records what the alternative costs:
    signing a re-serialized form of the body passes every unit test that
    mints its own body and fails every real request.
    """
    raw_body, headers = build_signed_request(
        secret=SECRET, ticket_id="12345", comment_id="cxforge-verify-abc"
    )
    verify_signature(
        secret=SECRET,
        timestamp=headers["X-Zendesk-Webhook-Signature-Timestamp"],
        signature=headers["X-Zendesk-Webhook-Signature"],
        raw_body=raw_body,
    )
    payload = ZendeskWebhookPayload.model_validate_json(raw_body)
    assert payload.ticket_id == "12345"
    assert payload.comment_id == "cxforge-verify-abc"


def test_a_body_altered_after_signing_no_longer_verifies() -> None:
    raw_body, headers = build_signed_request(
        secret=SECRET, ticket_id="12345", comment_id="cxforge-verify-abc"
    )
    with pytest.raises(SignatureVerificationError):
        verify_signature(
            secret=SECRET,
            timestamp=headers["X-Zendesk-Webhook-Signature-Timestamp"],
            signature=headers["X-Zendesk-Webhook-Signature"],
            raw_body=raw_body.replace(b"12345", b"99999"),
        )


def test_every_invocation_signs_a_different_comment_id() -> None:
    a, _ = build_signed_request(secret=SECRET, ticket_id="1", comment_id="x")
    from scripts.verify_core_loop import synthetic_comment_id

    assert synthetic_comment_id() != synthetic_comment_id()
    assert b"x" in a


# ---------------------------------------------------------------------------
# Logic half — which row counts as evidence
# ---------------------------------------------------------------------------


def _run_item(run_id: int, ticket_id: str, seconds: float = 7.0) -> dict[str, object]:
    received = datetime(2026, 8, 17, 5, 0, 0, tzinfo=UTC)
    return {
        "run_id": run_id,
        "ticket_id": ticket_id,
        "route": "case_status",
        "outcome": "auto_sent",
        "received_at": received.isoformat(),
        "replied_at": (received + timedelta(seconds=seconds)).isoformat(),
    }


def test_a_stale_row_for_the_same_ticket_is_not_accepted_as_evidence() -> None:
    """The false pass this check exists to make impossible.

    A dead worker plus one `runs` row left over from an earlier invocation
    must NOT read as a working core loop. Confirmed against a live stack as
    well as here: with the worker killed and a real matching row in the
    table, the check still failed.
    """
    stale = _run_item(6, "987654")
    assert find_new_run([stale], ticket_id="987654", baseline_run_ids={6}) is None


def test_a_new_row_for_a_different_ticket_is_not_accepted_either() -> None:
    other = _run_item(7, "some-other-ticket")
    assert find_new_run([other], ticket_id="987654", baseline_run_ids={6}) is None


def test_the_new_row_for_this_ticket_is_found_among_the_stale_ones() -> None:
    feed = [_run_item(6, "987654"), _run_item(7, "other"), _run_item(8, "987654")]
    found = find_new_run(feed, ticket_id="987654", baseline_run_ids={6, 7})
    assert found is not None and found["run_id"] == 8


# ---------------------------------------------------------------------------
# Logic half — whether the row that DID appear is real
# ---------------------------------------------------------------------------


def test_a_genuine_row_passes_and_reports_its_interval() -> None:
    assert assert_row_is_real(_run_item(8, "987654", seconds=7.5), ticket_id="987654") == 7.5


def test_the_22us_interval_that_adr_004_corrected_is_rejected() -> None:
    """`docs/STATE.md §4.1`: before Wave 1 `received_at` was minted inside
    `act`, the LAST graph node, so the interval timed only the tail-end
    helpdesk calls and measured 22µs across a 300ms ingest delay. A row
    existed the whole time. Existence is not evidence."""
    row = _run_item(8, "987654", seconds=0.000022)
    with pytest.raises(DeepCheckFailed, match="µs"):
        assert_row_is_real(row, ticket_id="987654")


def test_a_gated_run_is_rejected_rather_than_quietly_counted() -> None:
    """Gate ON writes `outcome = NULL` and `replied_at = NULL` by design
    (`agent.nodes.act`). Nothing was sent, so nothing is proven."""
    row = _run_item(8, "987654")
    row["outcome"] = None
    row["replied_at"] = None
    with pytest.raises(DeepCheckFailed, match="gate"):
        assert_row_is_real(row, ticket_id="987654")


def test_a_row_with_no_route_is_rejected() -> None:
    row = _run_item(8, "987654")
    row["route"] = None
    with pytest.raises(DeepCheckFailed, match="classify"):
        assert_row_is_real(row, ticket_id="987654")


def test_a_row_for_the_wrong_ticket_is_rejected() -> None:
    with pytest.raises(DeepCheckFailed, match="expected"):
        assert_row_is_real(_run_item(8, "someone-elses-ticket"), ticket_id="987654")


# ---------------------------------------------------------------------------
# Logic half — cleanup blast radius
# ---------------------------------------------------------------------------


def test_cleanup_deletes_only_this_invocations_rows() -> None:
    """`DELETE FROM runs WHERE ticket_id = …` would be shorter and would take
    out somebody else's run on a ticket that is deliberately reused."""
    statements = cleanup_statements("987654", "cxforge-verify-abc", run_id=8)
    joined = " ".join(statements)
    assert "DELETE FROM runs WHERE id = 8;" in joined
    assert "DELETE FROM drafts WHERE run_id = 8;" in joined
    assert "WHERE ticket_id = '987654' AND comment_id = 'cxforge-verify-abc'" in joined
    assert "FROM runs WHERE ticket_id" not in joined
    assert "FROM drafts WHERE run_id IN" not in joined


def test_cleanup_with_no_run_still_removes_the_dedup_row() -> None:
    """A failed check has still written `tickets_seen`. Leaving it means the
    next invocation's webhook comes back `duplicate: true` and enqueues
    nothing — the failure path would poison the retry."""
    statements = cleanup_statements("987654", "cxforge-verify-abc", run_id=None)
    assert len(statements) == 1
    assert statements[0].startswith("DELETE FROM tickets_seen")
