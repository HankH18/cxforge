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

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def test_env_example_deploy_host_is_still_a_bare_empty_assignment() -> None:
    lines = ENV_EXAMPLE.read_text().splitlines()
    deploy_host_lines = [ln for ln in lines if ln.startswith("DEPLOY_HOST=")]
    assert len(deploy_host_lines) == 1, deploy_host_lines
    assert deploy_host_lines[0] == "DEPLOY_HOST=", deploy_host_lines[0]


def test_env_example_still_declares_the_public_hostname_the_gate_reads() -> None:
    """``.env.example`` is where the public hostname is written down, and the
    public-path stage skips (loudly) when it is empty — so a run that has it
    undeclared cannot check Zendesk's route at all. Declared-and-empty is the
    correct shipped state; missing entirely is not."""
    declared = [
        ln for ln in ENV_EXAMPLE.read_text().splitlines() if ln.startswith("PUBLIC_BASE_URL=")
    ]
    assert len(declared) == 1, declared
