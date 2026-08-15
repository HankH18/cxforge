"""T-14 acceptance 4: docs/tickets.json carries a `status` field per ticket.

ORIGINAL PLAN DEFECT (T-14, v1 harness -- kept for history, see below for
its v2 resolution): acceptance 4 as written said "a status field the hooks
maintain". As of T-14, NONE of .claude/hooks/** wrote docs/tickets.json --
grep confirmed every reference to tickets.json under .claude/hooks/** was a
`jq` READ (verify_gate.sh, scope_guard.sh). verify_gate.sh wrote completion
evidence only to .claude/evidence/<id>.pass; claim.sh wrote only .claude/
active-ticket. Teaching a hook to write docs/tickets.json required editing
.claude/hooks/verify_gate.sh, outside T-14's scope -- so T-14 shipped the
`status` field hand-maintained, plus a ONE-DIRECTIONAL test (evidence
present => status must say "closed") that kept it honest for the one thing
mechanically checkable without touching a hook. The rest was left as an
explicit human decision.

RESOLUTION UNDER T-31 (harness-sync migration): the human decision was
never "teach a hook to write status" -- it was "delete the concept". The v2
lifecycle (.claude/scripts/harness_lib.py) does not store ticket status
anywhere. `status(tid)` is a pure function of two other files' existence:
    receipt exists (.claude/evidence/<id>.json) -> "resolved"
    a claim names the ticket (.claude/claims/<session>.json) -> "in_progress"
    neither -> "queue"
It never reads, and `cmd_claim`/`cmd_close`/`cmd_release` never write,
`docs/tickets.json`'s `status` field. `.claude/scripts/gen_tasks.py` (which
regenerates docs/TASKS.md, the human-facing board) imports and calls this
`status()` function, not the ticket dict's `status` key. Nothing in the
harness reads it. The field docs/tickets.json still carries (see
ALLOWED_STATUSES below -- it is still present, still one of "open"/
"in_progress"/"closed" for all 32 tickets today) is therefore DEAD: cosmetic
plan metadata a human typed once, never consulted by anything that gates a
claim, a close, or a guard decision, and free to go stale without breaking
the lifecycle. That is the defect closed by deletion rather than by
automation.

This module keeps every behaviour class the v1 tests covered, re-expressed
against the v2 reality:
  * schema well-formedness of the (now inert) field -- unchanged, still
    meaningful as a plan-hygiene check even though nothing consumes it;
  * the one-directional "local evidence implies closure" check -- kept
    intact, retargeted at what the v2 lifecycle actually consults
    (`harness_lib.status()`/`.claude/evidence/*.json`) instead of the dead
    tickets.json field, so it still catches a real regression: a future
    change that made evidence and derived status disagree;
  * a NEW pair of tests standing in for the v1 test's now-impossible
    converse: that the stored field is inert. A static check greps
    harness_lib.py's `status()` function body for any reference to the
    string "status" (there must be none); a dynamic check builds a
    synthetic project in tmp_path and proves, by construction, that toggling
    the stored field between "open" and "closed" does not move the derived
    status one bit -- only adding/removing a claim or receipt file does.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ._planlib import load_tickets

REPO_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_DIR = REPO_ROOT / ".claude" / "evidence"
HARNESS_LIB_PATH = REPO_ROOT / ".claude" / "scripts" / "harness_lib.py"

ALLOWED_STATUSES = {"open", "in_progress", "closed"}


def test_every_ticket_has_a_recognised_status() -> None:
    """Legacy schema check, unchanged from T-14: the field is still present
    and still one of a fixed vocabulary. Kept as plan hygiene even though
    (see module docstring) nothing in the v2 lifecycle reads it."""
    tickets = load_tickets()
    for t in tickets:
        assert "status" in t, f"{t['id']} is missing a status field"
        assert t["status"] in ALLOWED_STATUSES, (
            f"{t['id']} has status {t['status']!r}, not one of {sorted(ALLOWED_STATUSES)}"
        )


def test_status_agrees_with_local_evidence_where_evidence_exists() -> None:
    """One-directional, same shape as the v1 check it replaces (see module
    docstring): a ticket with a `.claude/evidence/<id>.json` receipt on THIS
    machine must derive as "resolved" via the real `harness_lib.py
    status_board` -- the v2 lifecycle's own status surface, not the dead
    tickets.json field. A ticket with no local receipt is unconstrained by
    this test (queue/in_progress here is not a contradiction; a fresh
    clone/CI has no .claude/evidence/ at all).

    Reads the real repo's live .claude/claims/ and .claude/evidence/ purely
    via the harness's own read-only `status_board` subcommand -- this test
    never writes to either directory, so it cannot disturb this session's
    own live T-31 claim.
    """
    if not EVIDENCE_DIR.exists():
        return  # fresh clone / CI: nothing local to check against
    receipted_ids = {p.stem for p in EVIDENCE_DIR.glob("*.json")}
    if not receipted_ids:
        return

    result = subprocess.run(
        [sys.executable, str(HARNESS_LIB_PATH), "status_board"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=30,
    )
    assert result.returncode == 0, f"status_board failed: {result.stderr!r}"
    derived: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("T-"):
            derived[parts[0]] = parts[1]

    for ticket_id in receipted_ids:
        if ticket_id not in derived:
            continue  # receipt for a ticket id no longer in the plan
        assert derived[ticket_id] == "resolved", (
            f"{ticket_id} has a receipt at .claude/evidence/{ticket_id}.json on "
            f"this machine but harness_lib.py status_board derives "
            f"{derived[ticket_id]!r}, not 'resolved' -- the v2 lifecycle's "
            f"claim/receipt state has gone inconsistent with its own evidence."
        )


def test_status_function_source_never_reads_the_tickets_json_field() -> None:
    """Static guard: harness_lib.py's `status()` function -- the ONE place
    that decides queue/in_progress/resolved for the whole lifecycle -- must
    derive purely from claims()/receipt(), never from the ticket dict's own
    `status` key. Extracts the function body by source text (from `def
    status(tid):` to the next top-level `def `) rather than importing the
    module, so this test cannot be fooled by a differently-named helper
    doing the reading elsewhere -- it inspects the actual function that
    gen_tasks.py and status_board call.
    """
    src = HARNESS_LIB_PATH.read_text()
    match = re.search(r"\ndef status\(tid\):\n(?:[ \t].*\n|\n)*", src)
    assert match, "could not locate harness_lib.py's status(tid) function source"
    body = match.group(0)
    assert '"status"' not in body and "'status'" not in body, (
        "status(tid) must never reference the tickets.json 'status' key -- "
        "found a 'status' string literal inside its body:\n" + body
    )


def _write_harness_copy(dst_scripts_dir: Path) -> None:
    dst_scripts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HARNESS_LIB_PATH, dst_scripts_dir / "harness_lib.py")


def _derived_status(project_dir: Path, ticket_id: str) -> str:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    harness_lib_path = project_dir / ".claude" / "scripts" / "harness_lib.py"
    result = subprocess.run(
        [sys.executable, str(harness_lib_path), "status_board"],
        capture_output=True,
        text=True,
        env=env,
        cwd=project_dir,
        timeout=30,
    )
    assert result.returncode == 0, f"status_board failed: {result.stderr!r}"
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] == ticket_id:
            return parts[1]
    raise AssertionError(f"{ticket_id} not found in status_board output: {result.stdout!r}")


def _make_ticket(status_value: str) -> dict:
    return {
        "id": "T-STATUSFIELD",
        "title": "synthetic ticket for the status-field dead-ness proof",
        "objective": "n/a",
        "acceptance": ["n/a"],
        "verify": "true",
        "scope": ["nope/**"],
        "depends_on": [],
        "non_goals": [],
        "parallel_safe": False,
        "status": status_value,
    }


def test_stored_status_field_does_not_move_the_derived_status(tmp_path: Path) -> None:
    """Dynamic proof (the v2 guarantee superseding the v1 test's converse):
    with claim/receipt state held fixed, flipping the stored `status` field
    between "closed" and "open" does not change what `status_board` derives
    -- only adding/removing a claim or receipt file does. This is the
    behavioural half of "the stored status field is not what the lifecycle
    consults": the static check above proves the source never reads it, this
    proves the field is causally inert end to end.
    """
    scripts_dir = tmp_path / ".claude" / "scripts"
    _write_harness_copy(scripts_dir)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "claims").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "evidence").mkdir(parents=True, exist_ok=True)

    def write_tickets(status_value: str) -> None:
        (tmp_path / "docs" / "tickets.json").write_text(
            json.dumps({"tickets": [_make_ticket(status_value)]})
        )

    # No claim, no receipt: derived status is "queue" regardless of the
    # stored field's value.
    write_tickets("closed")
    assert _derived_status(tmp_path, "T-STATUSFIELD") == "queue"
    write_tickets("open")
    assert _derived_status(tmp_path, "T-STATUSFIELD") == "queue"

    # A claim exists: derived status is "in_progress" regardless of the
    # stored field -- flip it to "closed" while a claim is live to prove the
    # claim, not the field, decides.
    (tmp_path / ".claude" / "claims" / "sess-a.json").write_text(
        json.dumps(
            {
                "ticket": "T-STATUSFIELD",
                "session": "sess-a",
                "note": "test",
                "start_commit": "deadbeef",
                "attempts": 0,
                "ts": 0,
            }
        )
    )
    write_tickets("closed")
    assert _derived_status(tmp_path, "T-STATUSFIELD") == "in_progress"

    # A receipt exists: derived status is "resolved" regardless of the
    # stored field -- flip it to "open" while a receipt is present to prove
    # the receipt, not the field, decides.
    (tmp_path / ".claude" / "claims" / "sess-a.json").unlink()
    (tmp_path / ".claude" / "evidence" / "T-STATUSFIELD.json").write_text(
        json.dumps(
            {
                "ticket": "T-STATUSFIELD",
                "session": "sess-a",
                "verify": "true",
                "commit": "deadbeef",
                "fingerprint": "x",
                "attempts": 0,
                "ts": 0,
            }
        )
    )
    write_tickets("open")
    assert _derived_status(tmp_path, "T-STATUSFIELD") == "resolved"
