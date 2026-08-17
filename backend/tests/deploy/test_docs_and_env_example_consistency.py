"""Precondition sanity check for the rest of backend/tests/deploy.

If ``.env.example``'s ``DEPLOY_HOST`` line ever stops being a bare empty
assignment, the regression scenario ``test_deploy_host_precedence.py``
reproduces (an empty, .env-defined ``DEPLOY_HOST`` silently clobbering an
already-exported one) would no longer match what a real, freshly-copied
``.env`` actually looks like. Flag that drift here rather than let it
silently invalidate this suite's premise — mirrors the precondition-sanity
pattern in backend/tests/evals/test_report.py.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
DEPLOY_DOC = REPO_ROOT / "docs" / "deploy.md"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_deploy.sh"


def test_env_example_deploy_host_is_still_a_bare_empty_assignment() -> None:
    lines = ENV_EXAMPLE.read_text().splitlines()
    deploy_host_lines = [ln for ln in lines if ln.startswith("DEPLOY_HOST=")]
    assert len(deploy_host_lines) == 1, deploy_host_lines
    assert deploy_host_lines[0] == "DEPLOY_HOST=", deploy_host_lines[0]


def test_deploy_doc_documents_the_local_opt_in_flag_the_script_actually_parses() -> None:
    """T-17 acceptance 4: docs/deploy.md must match the corrected precedence.

    The flag is read out of the script rather than hardcoded here, so renaming
    it in the script without updating the doc fails this test instead of
    silently leaving the doc describing an invocation that no longer works.
    """
    script = VERIFY_SCRIPT.read_text()
    assert "--local)" in script, "expected verify_deploy.sh to parse a --local flag"

    # A bare substring test would pass against a doc that only mentioned
    # "--localhost" or a renamed "--localXX", so anchor on a word boundary:
    # the flag must appear as its own token, the way a reader would type it.
    doc = DEPLOY_DOC.read_text()
    assert re.search(r"--local\b", doc), (
        "docs/deploy.md does not mention the --local opt-in flag that "
        "verify_deploy.sh requires for local mode"
    )


def test_deploy_doc_documents_the_public_path_stage_and_its_flag() -> None:
    """The public-path stage is worth nothing if a reader does not know the
    droplet-port assertions never touch Zendesk's route.

    Both halves are read out of the script rather than hardcoded, so renaming
    the flag or the variable fails this test instead of leaving the doc
    describing an invocation that no longer works — the same reason the
    ``--local`` test above is written this way.
    """
    script = VERIFY_SCRIPT.read_text()
    assert "--public)" in script, "expected verify_deploy.sh to parse a --public flag"
    assert "PUBLIC_BASE_URL" in script, (
        "verify_deploy.sh no longer reads PUBLIC_BASE_URL — if the public-path "
        "stage was removed, the gate is blind to the only route Zendesk has "
        "(docs/BUILD-PLAN.md §10.6g)"
    )

    doc = DEPLOY_DOC.read_text()
    assert re.search(r"--public\b", doc), (
        "docs/deploy.md does not mention the --public flag verify_deploy.sh parses"
    )
    assert "PUBLIC_BASE_URL" in doc, (
        "docs/deploy.md does not mention PUBLIC_BASE_URL, so nothing tells a "
        "reader that DEPLOY_HOST-based checks bypass Cloudflare entirely"
    )


def test_env_example_still_declares_the_public_hostname_the_gate_reads() -> None:
    """``.env.example`` is where the public hostname is written down, and the
    public-path stage skips (loudly) when it is empty — so a run that has it
    undeclared cannot check Zendesk's route at all. Declared-and-empty is the
    correct shipped state; missing entirely is not."""
    declared = [
        ln for ln in ENV_EXAMPLE.read_text().splitlines() if ln.startswith("PUBLIC_BASE_URL=")
    ]
    assert len(declared) == 1, declared


def test_deploy_doc_states_that_an_exported_deploy_host_wins_over_dotenv() -> None:
    """The precedence rule is the whole point of T-17; the doc must say so.

    Asserted on meaning-bearing terms rather than a quoted sentence, so
    rewording the prose does not fail the test but deleting the rule does.
    """
    doc = DEPLOY_DOC.read_text().lower()
    assert "precedence" in doc, "docs/deploy.md never explains DEPLOY_HOST precedence"
    assert "clobber" in doc or "overwrit" in doc, (
        "docs/deploy.md does not explain that .env cannot overwrite an "
        "already-exported DEPLOY_HOST"
    )
