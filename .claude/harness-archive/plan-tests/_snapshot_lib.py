"""Shared helpers for the T-26 structural-snapshot check (backend/tests/plan/).

Not a test module itself (pytest only collects ``test_*.py``); imported by
test_structural_snapshot.py and by update_structural_snapshot.py (the
regeneration script a legitimate plan amendment runs).

T-26's objective: T-14's commit silently added T-17 to T-11's depends_on,
outside its sanctioned changes, and nothing detected the structural edit to
an existing contract. This module defines exactly what "structural" means
here -- the four fields T-26 acceptance 2 names verbatim: ``scope``,
``depends_on``, ``verify``, ``acceptance`` -- and how a live docs/tickets.json
is compared against a committed snapshot of them.

Deliberately excluded, per T-26's own non_goals ("status fields are excluded
from the snapshot (T-22's hooks own them)") and by not being named in
acceptance 2's field list: ``status`` (dead -- see test_status_field.py),
``title``, ``objective``, ``refs``, ``non_goals``, ``parallel_safe``,
``priority``. Those are narrative/metadata, not the ticket's contract with
the harness; pinning them would make every copy-edit look like a structural
amendment and bury the signal this check exists to surface.

Self-contained on purpose (no import of ``_planlib`` -- mirrors that
module's own independence): ``update_structural_snapshot.py`` is meant to be
runnable directly as ``python3 backend/tests/plan/update_structural_snapshot.py``,
outside pytest's ``--import-mode=importlib`` package machinery, so this
module resolves its own paths rather than relying on a sibling's relative
import.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
TICKETS_PATH = REPO_ROOT / "docs" / "tickets.json"
SNAPSHOT_PATH = Path(__file__).resolve().parent / "ticket_structural_snapshot.json"

# The exact four fields T-26 acceptance 2 names as "structural fields".
STRUCTURAL_FIELDS = ("scope", "depends_on", "verify", "acceptance")


def load_tickets() -> list[dict[str, Any]]:
    """Read docs/tickets.json fresh (duplicated from _planlib.load_tickets
    rather than imported, for the self-containment reason in the module
    docstring)."""
    data = json.loads(TICKETS_PATH.read_text())
    return data["tickets"]


def structural_fields(ticket: dict[str, Any]) -> dict[str, Any]:
    """Extract exactly STRUCTURAL_FIELDS from one ticket dict."""
    return {k: ticket[k] for k in STRUCTURAL_FIELDS}


def build_snapshot(tickets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The full snapshot shape: ticket id -> its structural fields."""
    return {t["id"]: structural_fields(t) for t in tickets}


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, dict[str, Any]]:
    return json.loads(path.read_text())


def diff_snapshot(
    live_tickets: list[dict[str, Any]], snapshot: dict[str, dict[str, Any]]
) -> list[str]:
    """Every discrepancy between `live_tickets`' structural fields and
    `snapshot`, as human-readable strings. Empty list means they match
    exactly -- ticket-id sets identical AND every structural field on every
    shared ticket identical.

    Pure function of its two arguments (no file I/O, no REPO_ROOT reads) so
    it can be exercised against doctored, in-memory or tmp_path-round-tripped
    data without ever touching the real docs/tickets.json or the real
    committed snapshot -- exactly what T-26 acceptance 3's sabotage test
    requires.
    """
    problems: list[str] = []
    live = build_snapshot(live_tickets)

    live_ids = set(live)
    snapshot_ids = set(snapshot)

    for missing in sorted(snapshot_ids - live_ids):
        problems.append(f"{missing}: present in snapshot but no longer in docs/tickets.json")
    for added in sorted(live_ids - snapshot_ids):
        problems.append(f"{added}: new ticket in docs/tickets.json but missing from the snapshot")

    for tid in sorted(live_ids & snapshot_ids):
        live_fields = live[tid]
        snapshot_fields = snapshot[tid]
        for field in STRUCTURAL_FIELDS:
            live_value = live_fields.get(field)
            snapshot_value = snapshot_fields.get(field)
            if live_value != snapshot_value:
                problems.append(
                    f"{tid}.{field}: snapshot={snapshot_value!r} live={live_value!r}"
                )
    return problems
