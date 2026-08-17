"""T-14 acceptance 2: no ticket's verify invokes a script that same ticket
authors (the T-11 / verify_deploy.sh shape) -- a ticket authoring the SUITE
that gates it is normal TDD (exempt, per the test-runner-with-directory-
argument carve-out); a ticket authoring the GATE ITSELF is forbidden.

Uses the decidable tokenisation rule from T-14's contract verbatim:
tokenise the verify command, take every token that is a repo-relative path
or is an argument to bash/sh, and fail if any such path matches that
ticket's own scope globs -- except a directory argument to a test-runner
invocation (pytest/npm/uv run pytest).

RED-STATE NOTE (T-14 acceptance 6): written and run against the CURRENT,
unmodified docs/tickets.json to demonstrate failure BEFORE any verify
command is rewritten.
"""

from __future__ import annotations

import pytest

from ._planlib import find_self_gating_violations, load_tickets

_TICKETS = load_tickets()


@pytest.mark.parametrize("ticket", _TICKETS, ids=lambda t: t["id"])
def test_verify_does_not_self_gate(ticket: dict) -> None:
    violations = find_self_gating_violations(ticket)
    assert not violations, (
        f"{ticket['id']} verify {ticket['verify']!r} directly invokes "
        f"token(s) {violations}, which match its own scope globs "
        f"{ticket['scope']!r} -- a ticket must not author the gate that "
        f"verifies it (the T-11 / verify_deploy.sh shape). Test-runner "
        f"invocations with a directory argument are exempt; this is not "
        f"one of those."
    )
