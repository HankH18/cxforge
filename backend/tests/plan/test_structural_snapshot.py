"""T-26 acceptance 2 & 3: plan files are tamper-evident.

T-26's objective: T-14's commit silently added T-17 to T-11's depends_on,
outside T-14's sanctioned changes, and nothing detected the structural edit
to an existing contract. This module is the detector.

``backend/tests/plan/ticket_structural_snapshot.json`` is a COMMITTED
snapshot of every ticket's structural fields -- ``scope``, ``depends_on``,
``verify``, ``acceptance`` (exactly the four acceptance 2 names; see
``_snapshot_lib.STRUCTURAL_FIELDS`` for why ``status`` and every other field
are excluded). ``test_live_tickets_match_committed_snapshot`` below is
acceptance 2's own test: it fails the moment ``docs/tickets.json`` drifts
from that snapshot in any structural field, on any ticket.

AMENDMENT WORKFLOW (surfaced in the failure message below, not just here):
a legitimate change to any ticket's scope, depends_on, verify, or acceptance
must regenerate the snapshot in the SAME commit --
``python3 backend/tests/plan/update_structural_snapshot.py``, then commit
the updated ``ticket_structural_snapshot.json`` alongside the
``docs/tickets.json`` change. That is what turns a structural plan edit into
something a reviewer sees in the diff, instead of the silent T-14-on-T-11
shape that motivated this ticket. Acceptance 1's T-17 ratification is the
worked example already committed: T-11's snapshot entry carries T-17 in
``depends_on`` because that decision was made and recorded, not because it
went unnoticed.

ACCEPTANCE 3 ("prove the snapshot test actually bites"):
``test_snapshot_diff_catches_a_synthetic_silent_depends_on_edit`` below
never mutates the real ``docs/tickets.json``. It builds a doctored COPY of
the live tickets in ``tmp_path``, reproducing the exact T-14-on-T-11 shape
(a depends_on edge added with no corresponding snapshot update), round-trips
both the doctored tickets and the untouched baseline snapshot through real
files (so the same ``json.loads`` path the live check uses is exercised,
not just an in-memory dict compare), and asserts ``diff_snapshot`` flags it
-- plus a counter-check that the identical doctored tickets against a
snapshot that WAS updated to match reports clean, proving the detector
fails specifically because of the stale snapshot and not unconditionally.
"""

from __future__ import annotations

import json

from ._planlib import load_tickets
from ._snapshot_lib import SNAPSHOT_PATH, build_snapshot, diff_snapshot, load_snapshot

_REPO_ROOT_RELATIVE_SNAPSHOT_PATH = "backend/tests/plan/ticket_structural_snapshot.json"


def test_live_tickets_match_committed_snapshot() -> None:
    live_tickets = load_tickets()
    snapshot = load_snapshot()
    problems = diff_snapshot(live_tickets, snapshot)
    assert not problems, (
        "docs/tickets.json's structural fields (scope, depends_on, verify, "
        f"acceptance) have drifted from the committed snapshot at "
        f"{_REPO_ROOT_RELATIVE_SNAPSHOT_PATH} -- this is exactly the class of "
        "silent structural edit T-26 exists to catch (T-14's commit silently "
        "adding T-17 to T-11's depends_on, outside T-14's sanctioned changes). "
        "If this is a LEGITIMATE plan amendment: regenerate the snapshot with "
        "`python3 backend/tests/plan/update_structural_snapshot.py` and commit "
        "the updated snapshot IN THE SAME COMMIT as the docs/tickets.json "
        "change, so the amendment is legible in the diff instead of silent. "
        "Discrepancies:\n  " + "\n  ".join(problems)
    )


def test_snapshot_covers_every_ticket_id_present_live() -> None:
    """Cheap standalone sanity check, independent of the field-by-field diff
    above: no ticket can be silently dropped from (or added to only on one
    side of) the snapshot."""
    live_ids = {t["id"] for t in load_tickets()}
    snapshot_ids = set(load_snapshot())
    assert live_ids == snapshot_ids, (
        f"ticket id sets differ -- only live: {sorted(live_ids - snapshot_ids)}, "
        f"only snapshot: {sorted(snapshot_ids - live_ids)}"
    )


def test_snapshot_path_is_where_the_amendment_workflow_says_it_is() -> None:
    """The failure messages above and update_structural_snapshot.py's own
    docstring both tell a human/agent a specific repo-relative path to run
    and to look at -- guard that the constant they're built from doesn't
    silently drift out from under those instructions."""
    assert SNAPSHOT_PATH.exists()
    assert str(SNAPSHOT_PATH).endswith(_REPO_ROOT_RELATIVE_SNAPSHOT_PATH)


def test_snapshot_diff_catches_a_synthetic_silent_depends_on_edit(tmp_path) -> None:
    """T-26 acceptance 3. Never touches the real docs/tickets.json."""
    live_tickets = load_tickets()
    baseline_snapshot = load_snapshot()

    # Sabotage: silently add a dependency edge to one ticket's depends_on --
    # the T-14-on-T-11 shape -- WITHOUT updating the snapshot to match.
    doctored = [dict(t) for t in live_tickets]
    target = doctored[0]
    target["depends_on"] = [*target["depends_on"], "T-DOCTORED-INJECTED-DEP"]

    # Round-trip through real files in tmp_path so this exercises the same
    # json.loads() code path the live check uses, never writing to the real
    # docs/tickets.json or the real committed snapshot.
    doctored_tickets_path = tmp_path / "tickets.json"
    doctored_tickets_path.write_text(json.dumps({"tickets": doctored}))
    stale_snapshot_path = tmp_path / "ticket_structural_snapshot.json"
    stale_snapshot_path.write_text(json.dumps(baseline_snapshot))

    reloaded_tickets = json.loads(doctored_tickets_path.read_text())["tickets"]
    reloaded_snapshot = json.loads(stale_snapshot_path.read_text())

    problems = diff_snapshot(reloaded_tickets, reloaded_snapshot)

    assert problems, (
        "SABOTAGE CHECK FAILED: silently adding a depends_on edge to a "
        "doctored copy was not detected by diff_snapshot -- the snapshot "
        "check would be decorative, not tamper-evident"
    )
    assert any(target["id"] in p and "depends_on" in p for p in problems), (
        f"expected a depends_on discrepancy for {target['id']!r} in {problems}"
    )

    # Counter-check: the SAME doctored tickets against a snapshot that WAS
    # regenerated to match (the honest amendment workflow) reports clean --
    # proves the detector fails because the snapshot is stale, not because
    # it always fails.
    updated_snapshot = build_snapshot(doctored)
    assert diff_snapshot(doctored, updated_snapshot) == []
