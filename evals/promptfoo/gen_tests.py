"""promptfoo dynamic test generation for the classify suite (W1-E1).

The routing cases are **derived from ``evals/labeled_set.yaml``** — the
human-approved ground truth (``approval.status: APPROVED``, Hank Holcomb,
2026-08-15) — rather than hand-copied into YAML. Copying would create a second,
drifting set of labels next to the approved one; deriving keeps exactly one
source of truth for what the right answer is.

SELECTION (deterministic, and deliberately small)
-------------------------------------------------
Every promptfoo test costs one live Opus call, and this suite is meant to be
re-run often, so it is a *canonical-scenario smoke suite*, not a census. The
census is ``evals/route_accuracy.py``, which sweeps all 51.

* the first ``PER_ROUTE`` labeled tickets of each of the four canonical
  scenarios (``case_status`` / ``permission`` / ``kb`` / ``off_topic``), in
  file order; plus
* every ticket whose escalation is **route dependent** — where the condition
  that fires it is detected inside one specific branch node, so a mis-route
  silently means no escalation at all. ``evals.route_accuracy.
  required_branch_route`` decides which those are and what route they need;
  this module does not re-derive that rule.

``classify`` can never emit ``"escalate"`` (its schema is typed against
``agent.state.ClassifyRoute``), so the expected value for a route-dependent
escalate ticket is its required *branch* route, never ``"escalate"``.
"""

from __future__ import annotations

# _bootstrap must be imported before anything that needs a repo dependency,
# because it re-execs into the venv interpreter. Sorting these imports would
# silently break every promptfoo run on a machine whose default `python` is
# not this repo's — hence isort is switched off for this block.
# isort: off
import sys
from pathlib import Path

# See provider.py's header comment: promptfoo loads this file outside a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _bootstrap  # noqa: E402, F401  (MUST be first — re-execs the interpreter)

from typing import Any  # noqa: E402

from evals.report import load_labeled_set  # noqa: E402
from evals.route_accuracy import BRANCH_ROUTES, required_branch_route  # noqa: E402

PER_ROUTE = 3


def _customer_message(ticket: dict[str, Any]) -> str:
    """The same text ``evals.report.build_ticket_and_conversation`` puts in the
    conversation's single customer message, so promptfoo and the E3 sweep are
    measuring the same input."""
    return str(ticket["body"]).strip()


def _case(ticket: dict[str, Any], expected_route: str, note: str) -> dict[str, Any]:
    return {
        "description": f"classify/{ticket['id']} -> {expected_route} ({note})",
        "vars": {
            "suite": "classify",
            "message": _customer_message(ticket),
            "ticket_id": ticket["id"],
            "expected_route": expected_route,
        },
        "assert": [
            {"type": "is-json"},
            {
                # promptfoo wraps a single-line `javascript` value in an implicit
                # `return`, so this must be an EXPRESSION — a statement body
                # (`const x = ...; return ...`) raises "Unexpected token 'return'"
                # and every case reports as failed for the wrong reason. Measured
                # against promptfoo 0.122.0 on 2026-08-16.
                "type": "javascript",
                "value": f"JSON.parse(output).route === {expected_route!r}",
            },
        ],
    }


def generate_classify_tests() -> list[dict[str, Any]]:
    _, tickets = load_labeled_set()
    cases: list[dict[str, Any]] = []

    for route in BRANCH_ROUTES:
        matching = [t for t in tickets if t.get("expected_route") == route]
        for ticket in matching[:PER_ROUTE]:
            cases.append(_case(ticket, route, "canonical scenario"))

    for ticket in tickets:
        if ticket.get("expected_route") != "escalate":
            continue
        needed = required_branch_route(ticket)
        if needed is None:
            continue
        cases.append(_case(ticket, needed, "route-dependent escalation"))

    if not cases:
        raise RuntimeError(
            "generate_classify_tests produced no cases — evals/labeled_set.yaml is not "
            "shaped the way this generator expects. Refusing to hand promptfoo an empty "
            "suite, which would report a vacuous pass."
        )
    return cases

# isort: on
