"""Customer-facing wording changes that had to not cost anything (R9).

Two owner decisions land in ``agent.templates``:

* **ADR-011 (W2-B3)** — the permission reply stops claiming *"it's now been
  processed for your case"*, an action the codebase performs nowhere.
* **ADR-020 (W2-B5)** — a case-status reply that states an ETA carries a
  short "estimated timeline, subject to change" qualifier, because the live
  model routes exact-date questions here (correctly) and the honest answer
  to "what is the EXACT date" is a week estimate said without implying
  precision the lab does not have.

Both are prose edits to the one module where R9 is enforced, so the tests
that matter are not "does the new string appear" — they are **does the new
prose introduce a claim**. ``agent.grounding_guard`` is the project's
deterministic, judge-independent detector for case-fact-shaped assertions;
these tests run the new wording through it directly, including in the
harshest setting the guard ever sees (free-generated text with no resolved
case at all), and require the answer to be "nothing new".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agent import templates
from agent.grounding_guard import (
    extract_case_ids_loose,
    extract_dna_availability_claim,
    extract_eta_weeks_claims,
    extract_photos_availability_claim,
    extract_stage_claims,
    find_ungrounded_case_claims,
    has_personalizing_cue,
)
from agent.state import AlwaysGrantKind
from data import Case, get_case
from data.seed import SeedResult

from .conftest import assert_no_case_facts_present

pytestmark = pytest.mark.grounding

# fixtures/cases.yaml — extraction stage, a real ETA, so the reply below
# genuinely states a timeline.
_CASE_ID = "MFG-2025-0734"

ALL_GRANT_KINDS: list[AlwaysGrantKind] = [
    "add_authorized_contact",
    "resend_report",
    "extend_retention",
]


def _in_progress_case(seeded: SeedResult) -> Case:
    case = get_case(_CASE_ID)
    assert isinstance(case, Case)
    assert case.stage != "complete"
    assert case.eta_weeks is not None
    return case


def _completed_case(case: Case) -> Case:
    """The same case moved to ``complete``. ``fixtures/cases.yaml`` has no
    completed case, and inventing one in the fixture file would change data
    other suites read — so this is derived from a real row rather than
    hand-built."""
    return case.model_copy(
        update={"stage": "complete", "eta_weeks": 0, "last_updated": datetime.now(UTC)}
    )


def _fact_signature(text: str) -> tuple[Any, ...]:
    """Everything ``agent.grounding_guard`` can see in ``text``. Two strings
    with the same signature assert the same set of case facts, whatever else
    differs between them."""
    return (
        sorted(extract_case_ids_loose(text)),
        sorted(extract_stage_claims(text)),
        sorted(extract_eta_weeks_claims(text)),
        extract_dna_availability_claim(text),
        extract_photos_availability_claim(text),
    )


# -- ADR-020: the ETA qualifier ----------------------------------------------


def test_a_reply_that_states_an_eta_carries_the_qualifier(seeded: SeedResult) -> None:
    case = _in_progress_case(seeded)

    reply = templates.render_case_status_reply(case)

    assert "an estimated timeline, and subject to change" in reply
    # ...attached to the sentence it qualifies, not appended as a footer.
    eta_line = next(line for line in reply.splitlines() if "more week(s)" in line)
    assert "an estimated timeline, and subject to change" in eta_line


def test_a_completed_case_states_no_timeline_and_carries_no_qualifier(
    seeded: SeedResult,
) -> None:
    """Scoped, per ADR-020: a completed case has no forward-looking estimate
    to hedge. Qualifying a statement of fact would train customers to skip
    the words in the replies where they carry weight."""
    reply = templates.render_case_status_reply(_completed_case(_in_progress_case(seeded)))

    assert "subject to change" not in reply
    assert "more week(s)" not in reply


def test_the_qualifier_asserts_nothing_the_grounding_guard_can_detect() -> None:
    """The qualifier's text, judged the way the guard judges free-generated
    prose with no resolved case behind it — the harshest setting it has. A
    qualifier that named a number, a stage, or a cause would produce a
    violation here, and would deserve to."""
    qualifier = templates._ETA_QUALIFIER

    assert find_ungrounded_case_claims(qualifier, {}) == []
    assert not has_personalizing_cue(qualifier)
    assert _fact_signature(qualifier) == ([], [], [], None, None)


def test_the_qualifier_changes_no_fact_the_guard_reads_off_the_reply(
    seeded: SeedResult,
) -> None:
    """The property that actually matters: adding the qualifier must leave
    every case fact the guard extracts from the whole reply *identical*.

    An empty-violations assertion alone could pass for the wrong reason (the
    guard simply not looking); comparing signatures before and after is a
    difference measurement, so it fails if the qualifier adds a stage word, a
    second week-number, or an availability-shaped phrase.
    """
    case = _in_progress_case(seeded)
    with_qualifier = templates.render_case_status_reply(case)
    without_qualifier = with_qualifier.replace(f" — {templates._ETA_QUALIFIER}", "")

    assert without_qualifier != with_qualifier, "the fixture edit no longer removes anything"
    assert _fact_signature(with_qualifier) == _fact_signature(without_qualifier)

    # And with the run's real tool result, the whole reply is still clean.
    assert find_ungrounded_case_claims(with_qualifier, {"case": case}) == []


# -- ADR-011: the permission reply -------------------------------------------


@pytest.mark.parametrize("kind", ALL_GRANT_KINDS)
def test_permission_reply_no_longer_claims_a_completed_side_effect(
    kind: AlwaysGrantKind,
) -> None:
    """ADR-011. The old copy said the request "has now been processed for
    your case"; nothing in this codebase processes anything. The reply may
    claim the approval — that is what the ``permission`` node really
    decided — and must not claim the execution."""
    reply = templates.render_permission_grant_reply(kind)

    assert "processed for your case" not in reply
    assert "approved" in reply
    # Says who actually applies the change, so "nothing happened" is
    # detectable by the customer rather than silently assumed done.
    assert "if you don't see it take effect" in reply


@pytest.mark.parametrize("kind", ALL_GRANT_KINDS)
def test_permission_reply_still_states_no_case_fact(
    kind: AlwaysGrantKind, fixture_cases: list[dict[str, Any]]
) -> None:
    """The rewrite must not have smuggled in case-specific content. The
    permission template has never stated a case fact and still must not —
    ``backend/tests/graph/test_canonical_scenarios.py`` asserts the sent body
    contains neither the case's stage nor its ETA, and this is the same
    property checked structurally over every real fixture case."""
    assert_no_case_facts_present(templates.render_permission_grant_reply(kind), fixture_cases)
