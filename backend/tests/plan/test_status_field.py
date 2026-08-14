"""T-14 acceptance 4: docs/tickets.json carries a `status` field per ticket.

PLAN DEFECT (recorded here, not routed around -- see T-14's handoff report):
acceptance 4 as written says "a status field the hooks maintain". As of this
ticket, NONE of .claude/hooks/** writes docs/tickets.json -- grep confirms
every reference to tickets.json under .claude/hooks/** is a `jq` READ
(verify_gate.sh, scope_guard.sh). verify_gate.sh writes completion evidence
only to .claude/evidence/<id>.pass; claim.sh writes only .claude/active-
ticket. Teaching a hook to write docs/tickets.json requires editing
.claude/hooks/verify_gate.sh (and arguably claim.sh), which is outside
T-14's file scope (docs/tickets.json, docs/TASKS.md, backend/tests/plan/**,
scripts/render_tasks_md.py) -- and T-14's own non-goals forbid widening that
scope to unblock itself. This is a human decision (amend T-14's scope, or
raise a follow-up ticket teaching verify_gate.sh to write `status` next to
the `.pass` file it already produces), not something this test routes
around.

What IS implemented, entirely within scope: the `status` field itself (set
by hand for this ticket, reflecting the real state of
.claude/evidence/*.pass and .claude/active-ticket at write time), plus the
test below, which keeps it HONEST going forward for the one thing it can
mechanically check without touching a hook.

Design note on why the check below is ONE-DIRECTIONAL (evidence-present =>
status must be "closed", not the converse): .claude/evidence/ is
gitignored (see .gitignore: "`.claude/evidence/`") -- it is local,
ephemeral, per-machine proof, never committed. On a fresh clone or in CI,
the directory does not exist at all, so a ticket legitimately marked
"closed" in the committed docs/tickets.json will have NO local evidence
file there. Requiring "closed" to imply an evidence file would make this
test fail on every environment except the one machine that happened to run
verify_gate.sh most recently -- not reproducible, and not what this test is
for. The direction that IS reproducible and IS exactly what acceptance 4
is protecting against: if a `.pass` file for a ticket exists on THIS
machine right now, docs/tickets.json must already say "closed" for it --
catching the drift the moment a ticket is closed locally without a human
remembering to flip the field (since nothing else will).
"""

from __future__ import annotations

from pathlib import Path

from ._planlib import load_tickets

REPO_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_DIR = REPO_ROOT / ".claude" / "evidence"

ALLOWED_STATUSES = {"open", "in_progress", "closed"}


def test_every_ticket_has_a_recognised_status() -> None:
    tickets = load_tickets()
    for t in tickets:
        assert "status" in t, f"{t['id']} is missing a status field"
        assert t["status"] in ALLOWED_STATUSES, (
            f"{t['id']} has status {t['status']!r}, not one of {sorted(ALLOWED_STATUSES)}"
        )


def test_status_agrees_with_local_evidence_where_evidence_exists() -> None:
    """One-directional (see module docstring): a ticket with a passing
    .claude/evidence/<id>.pass file on THIS machine must be marked
    "closed". A ticket with no local evidence file is not constrained by
    this test (it may legitimately be "closed" on a fresh clone/CI where
    evidence never existed, or "open"/"in_progress" here)."""
    if not EVIDENCE_DIR.exists():
        return  # fresh clone / CI: nothing local to check against
    tickets = {t["id"]: t for t in load_tickets()}
    for pass_file in EVIDENCE_DIR.glob("*.pass"):
        ticket_id = pass_file.stem
        if ticket_id not in tickets:
            continue  # evidence for a ticket id no longer in the plan
        status = tickets[ticket_id]["status"]
        assert status == "closed", (
            f"{ticket_id} has a passing {pass_file.relative_to(REPO_ROOT)} on "
            f"this machine but docs/tickets.json marks it {status!r}, not "
            f"'closed'. Nothing currently writes this field automatically "
            f"(see this module's PLAN DEFECT note) -- update it by hand to "
            f"match."
        )
