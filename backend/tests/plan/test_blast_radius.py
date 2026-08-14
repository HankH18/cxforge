"""T-14 acceptance 1: every ticket's verify runs its own suite plus every
suite that imports (directly or transitively) from its scope paths -- a
reverse-dependency set computed from the real, current import graph -- or
runs the full backend suite, which trivially satisfies the invariant.

RED-STATE NOTE (T-14 acceptance 6): written and run against the CURRENT,
unmodified docs/tickets.json to demonstrate failure BEFORE any verify
command is rewritten. Do not weaken this to make today's tickets pass --
that happens in a later phase of T-14, not here.
"""

from __future__ import annotations

import pytest

from ._planlib import (
    ImportGraph,
    analyze_verify,
    build_import_graph,
    load_tickets,
    required_test_dirs_for_ticket,
    scope_needs_npm_portal,
)

_TICKETS = load_tickets()
_GRAPH: ImportGraph = build_import_graph()


@pytest.mark.parametrize("ticket", _TICKETS, ids=lambda t: t["id"])
def test_verify_covers_its_blast_radius(ticket: dict) -> None:
    required = required_test_dirs_for_ticket(ticket, _GRAPH)
    needs_npm = scope_needs_npm_portal(ticket["scope"])
    cov = analyze_verify(ticket["verify"])

    missing_dirs = set() if cov.is_full_suite else (required - cov.covered_test_dirs)
    missing_npm = needs_npm and not cov.covers_npm_portal

    assert not missing_dirs and not missing_npm, (
        f"{ticket['id']} verify {ticket['verify']!r} does not cover its "
        f"blast radius. reverse-dependency set (own scope + every suite "
        f"that imports from it) = {sorted(required)}; verify only runs "
        f"{sorted(cov.covered_test_dirs)}"
        f"{' (full suite)' if cov.is_full_suite else ''}. "
        f"missing test suite(s): {sorted(missing_dirs)}."
        + (" missing npm (portal frontend) coverage." if missing_npm else "")
    )
