#!/usr/bin/env python3
"""Regenerate backend/tests/plan/ticket_structural_snapshot.json from the
live docs/tickets.json.

This is T-26's "amendment workflow": whenever a legitimate change touches
any ticket's scope, depends_on, verify, or acceptance, run this script and
commit its output IN THE SAME COMMIT as the docs/tickets.json change. That
turns a structural plan edit into something visible in the diff, instead of
the silent T-14-on-T-11 shape (T-17 quietly added to depends_on, outside
T-14's sanctioned changes) that motivated this ticket.

Run directly, not through pytest:
    python3 backend/tests/plan/update_structural_snapshot.py

Deliberately does not import `_planlib` or use a package-relative import
(see _snapshot_lib.py's module docstring) so it works whether or not the
caller has `backend/tests/plan` on sys.path already -- it adds its own
directory to sys.path itself, then imports `_snapshot_lib` as a plain
top-level module.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _snapshot_lib import SNAPSHOT_PATH, build_snapshot, load_tickets  # noqa: E402


def main() -> None:
    snapshot = build_snapshot(load_tickets())
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=1) + "\n")
    print(f"wrote {SNAPSHOT_PATH} ({len(snapshot)} tickets)")


if __name__ == "__main__":
    main()
