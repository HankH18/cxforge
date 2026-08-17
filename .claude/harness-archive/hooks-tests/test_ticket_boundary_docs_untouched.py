"""T-22 acceptances 2 and 3: end-to-end proof that a real ticket boundary --
claim, then close -- never requires (or performs) an agent-side edit of
docs/tickets.json or docs/TASKS.md.

CONTEXT (see backend/tests/plan/test_status_field.py's module docstring for
the full history): T-22 was written against the v1 harness, where
.claude/hooks/claim.sh and verify_gate.sh were real programs that hand-wrote
a `status` field into docs/tickets.json at each ticket boundary. Commit
c44f9af ("cc-factory: harness sync") replaced that with the v2 python
harness (.claude/scripts/harness_lib.py): status is DERIVED from
.claude/claims/*.json and .claude/evidence/*.json, never stored, and
docs/TASKS.md is regenerated wholesale by .claude/scripts/gen_tasks.py, which
cmd_close invokes automatically after every successful close. Neither
cmd_claim nor cmd_close ever opens docs/tickets.json for writing -- grep
confirms harness_lib.py's only touches to TICKETS are `load_tickets()`
(read) and the `ticket(tid)` lookup built on it.

Acceptance 2 ("hook writes are surgical") is proven here in its STRONGER v2
form: not merely that a boundary changes docs/tickets.json by exactly one
ticket's status value (the v1 shape), but that it changes docs/tickets.json
by NOTHING AT ALL -- byte-identical before and after both a claim and a
close. Acceptance 3 (end-to-end, both transitions) is proven by driving a
real claim and a real close against a synthetic fixture project via the real
(synthetic-project-local) .claude/scripts/claim.sh, then asserting the
resulting docs/tickets.json and docs/TASKS.md agree: TASKS.md is exactly
what an independent, later run of gen_tasks.py produces from the same
post-close state, with no agent edit of either file anywhere in the flow.

Self-contained per this package's established pattern (see conftest.py):
built on the `make_project`/`run_claim_sh` helpers, which build a disposable
git repo in tmp_path seeded with copies of the real .claude/scripts/ and
.claude/hooks/ trees, and which assert after every subprocess call that the
REAL repo's .claude/claims/, .claude/evidence/ and git HEAD never moved --
this session holds a LIVE claim on T-22 in the real repo right now.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .conftest import make_project, run_claim_sh

# A minimal, self-contained ticket -- deliberately NOT one of the real plan's
# 32 tickets, so this test's claim/close cycle can never collide with (or be
# confused for) a real ticket id. Scope names a directory that never exists
# in the fixture project, so the close-time integrity check and the
# fingerprint both operate over an empty file set; verify is a trivial `true`
# so cmd_close's real verify-and-close path runs for real without needing
# any product code.
BOUNDARY_TICKET: dict[str, Any] = {
    "id": "T-BOUNDARY",
    "title": "synthetic ticket for the T-22 ticket-boundary docs-untouched proof",
    "objective": "n/a -- exists only to drive a real claim/close cycle in a test",
    "acceptance": ["n/a"],
    "verify": "true",
    "scope": ["nonexistent-dir/**"],
    "depends_on": [],
    "non_goals": [],
    "parallel_safe": False,
    "status": "open",
}

TICKETS_DOC = {"tickets": [BOUNDARY_TICKET]}

SESSION_ID = "t22-boundary-test-session"


def _run_gen_tasks(project_dir: Path) -> str:
    """Independently re-invoke the real (synthetic-project-local)
    gen_tasks.py against whatever docs/tickets.json + .claude/claims/ +
    .claude/evidence/ state is currently on disk, and return the
    docs/TASKS.md it produces. Used to prove the file cmd_close already
    wrote is exactly what the generator produces from the same state --
    i.e. that TASKS.md and tickets.json "agree" -- rather than merely
    trusting cmd_close's own internal call to have done the right thing.
    """
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    result = subprocess.run(
        [sys.executable, str(project_dir / ".claude" / "scripts" / "gen_tasks.py")],
        capture_output=True,
        text=True,
        env=env,
        cwd=project_dir,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"gen_tasks.py failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return (project_dir / "docs" / "TASKS.md").read_text()


def test_claim_and_close_leave_tickets_json_byte_identical(tmp_path: Path) -> None:
    """Acceptance 2: docs/tickets.json is byte-identical before the claim,
    after the claim, and after the close. Not "differs by exactly one
    status value" (the v1/literal acceptance-2 shape) -- under v2 the
    stronger true statement is that it does not differ AT ALL, because
    cmd_claim and cmd_close never open the file for writing.
    """
    project_dir = make_project(tmp_path, TICKETS_DOC)
    tickets_path = project_dir / "docs" / "tickets.json"
    before = tickets_path.read_bytes()

    claim_result = run_claim_sh(
        project_dir, "T-BOUNDARY", note="T-22 boundary proof", session_id=SESSION_ID
    )
    assert claim_result.returncode == 0, (
        f"claim failed: stdout={claim_result.stdout!r} stderr={claim_result.stderr!r}"
    )
    after_claim = tickets_path.read_bytes()
    assert after_claim == before, (
        "claim.sh must not touch docs/tickets.json at all -- cmd_claim only "
        "reads it via load_tickets()/ticket(); it never opens it for writing"
    )

    close_result = run_claim_sh(
        project_dir, "T-BOUNDARY", subcommand="close", session_id=SESSION_ID
    )
    assert close_result.returncode == 0, (
        f"close failed: stdout={close_result.stdout!r} stderr={close_result.stderr!r}"
    )
    after_close = tickets_path.read_bytes()
    assert after_close == before, (
        "close.sh must not touch docs/tickets.json at all -- cmd_close only "
        "reads it (via the same load_tickets()/ticket() and, for the "
        "fingerprint, git-ls-files over the ticket's scope); status is "
        "derived from .claude/claims/ and .claude/evidence/, never written "
        "back into the ticket dict"
    )


def test_boundary_end_to_end_tickets_and_tasks_agree_with_no_agent_edit(
    tmp_path: Path,
) -> None:
    """Acceptance 3: drive both transitions end-to-end against a fixture
    project and assert docs/tickets.json and docs/TASKS.md agree afterwards
    -- TASKS.md equals what gen_tasks.py independently produces from the
    post-close state -- with no agent edit of either file anywhere in the
    flow (this test only ever invokes the real claim.sh/gen_tasks.py
    subprocesses; it never calls Edit/Write on either file itself).
    """
    project_dir = make_project(tmp_path, TICKETS_DOC)
    tickets_path = project_dir / "docs" / "tickets.json"
    tasks_path = project_dir / "docs" / "TASKS.md"
    receipt_path = project_dir / ".claude" / "evidence" / "T-BOUNDARY.json"
    tickets_before = tickets_path.read_bytes()

    claim_result = run_claim_sh(
        project_dir, "T-BOUNDARY", note="T-22 e2e proof", session_id=SESSION_ID
    )
    assert claim_result.returncode == 0, claim_result.stdout + claim_result.stderr
    assert not tasks_path.exists(), (
        "docs/TASKS.md is a CLOSE-boundary artifact (T-16): claim must not "
        "create or touch it"
    )
    assert not receipt_path.exists(), "claim must not mint a receipt"

    close_result = run_claim_sh(
        project_dir, "T-BOUNDARY", subcommand="close", session_id=SESSION_ID
    )
    assert close_result.returncode == 0, close_result.stdout + close_result.stderr
    assert tickets_path.read_bytes() == tickets_before, (
        "docs/tickets.json must still be untouched after close"
    )
    assert receipt_path.exists(), "close must mint a fingerprint-bound receipt"
    assert tasks_path.exists(), (
        "close must regenerate docs/TASKS.md via gen_tasks.py -- see "
        "cmd_close's final lines in harness_lib.py"
    )

    produced_by_close = tasks_path.read_text()

    # Independently re-derive TASKS.md from the SAME post-close on-disk
    # state (docs/tickets.json + .claude/claims/ [now empty] +
    # .claude/evidence/T-BOUNDARY.json) and assert byte-for-byte agreement.
    # This is "tickets.json and TASKS.md agree" made concrete: TASKS.md IS
    # gen_tasks.py's pure function of that state, not a hand-patched file
    # that merely happens to look right.
    regenerated = _run_gen_tasks(project_dir)
    assert regenerated == produced_by_close, (
        "docs/TASKS.md as left by cmd_close does not match what an "
        "independent gen_tasks.py run produces from the same post-close "
        "state -- TASKS.md and tickets.json have drifted"
    )

    assert "### T-BOUNDARY: " in regenerated, "the ticket must appear in TASKS.md"
    assert "`[resolved]`" in regenerated, (
        "a closed ticket with a receipt must derive as 'resolved' in the "
        "regenerated TASKS.md"
    )
