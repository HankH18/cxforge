"""T-7: the labeled-set fixture itself — DESIGN §Data models pins its shape
(``{id, subject, body, expected_route, expected_escalate,
expected_reasons[]}``) and docs/tickets.json T-7's acceptance pins its
coverage (all five routes, every DESIGN hard trigger, fuzzy
frustration/complexity cases, adversarial phrasing) and its human-approval
gate (never self-approved by the coding agent that authored the labels).

No LLM, no DB: everything here is a pure parse/shape/coverage check over
``evals/labeled_set.yaml`` and the pinned ``Reason``/route literals.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml

from escalation.schemas import Reason

REPO_ROOT = Path(__file__).resolve().parents[3]
LABELED_SET_PATH = REPO_ROOT / "evals" / "labeled_set.yaml"

VALID_REASONS = set(get_args(Reason))
VALID_ROUTES = {"case_status", "permission", "kb", "off_topic", "escalate"}

# DESIGN §Escalation contract's seven hard triggers. Three of them
# (unknown_case, out_of_procedure, low_confidence) map onto the pinned
# ``Reason`` literal directly; low_confidence itself collapses THREE
# distinct scenarios (empty retrieval, verifier failure, classifier
# abstention — see backend/src/escalation/rules.py's module docstring), so
# those three are only distinguishable via the id-naming convention
# documented in evals/labeled_set.yaml's ``meta.id_convention``.
HARD_TRIGGER_REASON_NAMES = {
    "billing",
    "human_request",
    "unknown_case",
    "out_of_procedure",
}
LOW_CONFIDENCE_SUBTYPE_ID_MARKERS = {
    "empty_retrieval",
    "verifier_failure",
    "abstention",
}


@pytest.fixture(scope="module")
def raw_document() -> dict[str, Any]:
    return yaml.safe_load(LABELED_SET_PATH.read_text())


@pytest.fixture(scope="module")
def tickets(raw_document: dict[str, Any]) -> list[dict[str, Any]]:
    return raw_document["tickets"]


# -- loader / shape ----------------------------------------------------------


def test_file_parses_as_yaml_with_expected_top_level_keys(raw_document: dict[str, Any]) -> None:
    assert "approval" in raw_document
    assert "tickets" in raw_document
    assert isinstance(raw_document["tickets"], list)
    assert len(raw_document["tickets"]) > 0


def test_every_ticket_has_the_pinned_fields(tickets: list[dict[str, Any]]) -> None:
    required = {"id", "subject", "body", "expected_route", "expected_escalate", "expected_reasons"}
    for ticket in tickets:
        missing = required - ticket.keys()
        assert not missing, f"{ticket.get('id', '<no id>')} missing fields: {missing}"
        assert isinstance(ticket["id"], str) and ticket["id"]
        assert isinstance(ticket["subject"], str) and ticket["subject"]
        assert isinstance(ticket["body"], str) and ticket["body"].strip()
        assert isinstance(ticket["expected_escalate"], bool)
        assert isinstance(ticket["expected_reasons"], list)


def test_ids_are_unique(tickets: list[dict[str, Any]]) -> None:
    ids = [t["id"] for t in tickets]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"duplicate ticket ids: {duplicates}"


# -- schema validity against the pinned literals ------------------------------


def test_expected_route_is_always_a_valid_route(tickets: list[dict[str, Any]]) -> None:
    for t in tickets:
        assert t["expected_route"] in VALID_ROUTES, t["id"]


def test_expected_reasons_are_always_from_the_pinned_reason_literal(
    tickets: list[dict[str, Any]],
) -> None:
    for t in tickets:
        for reason in t["expected_reasons"]:
            assert reason in VALID_REASONS, f"{t['id']}: {reason!r} not in {VALID_REASONS}"


def test_route_escalate_invariant(tickets: list[dict[str, Any]]) -> None:
    """The live graph (agent.nodes.decide/_escalate_for) always overwrites
    ``route`` to ``"escalate"`` once any hard rule or thresholded classifier
    verdict fires, and never for any other reason — so a labeled ticket's
    ``expected_route`` must be ``"escalate"`` if and only if
    ``expected_escalate`` is true."""
    for t in tickets:
        if t["expected_escalate"]:
            assert t["expected_route"] == "escalate", (
                f"{t['id']}: expected_escalate=True but expected_route={t['expected_route']!r}"
            )
        else:
            assert t["expected_route"] != "escalate", (
                f"{t['id']}: expected_route='escalate' but expected_escalate=False"
            )


def test_escalating_tickets_have_at_least_one_reason(tickets: list[dict[str, Any]]) -> None:
    for t in tickets:
        if t["expected_escalate"]:
            assert t["expected_reasons"], f"{t['id']}: escalate=True but no expected_reasons"


def test_non_escalating_tickets_have_no_reasons(tickets: list[dict[str, Any]]) -> None:
    for t in tickets:
        if not t["expected_escalate"]:
            assert t["expected_reasons"] == [], f"{t['id']}: escalate=False but has reasons"


# -- size ----------------------------------------------------------------


def test_set_size_is_within_the_ticket_defined_bounds(tickets: list[dict[str, Any]]) -> None:
    # docs/tickets.json T-7: "~50 tickets" (acceptance), "no expanding past
    # ~60" (non-goal). A generous but real floor/ceiling, not an exact 50.
    assert 40 <= len(tickets) <= 60, len(tickets)


# -- coverage requirements (docs/tickets.json T-7 acceptance) -----------------


def test_all_five_routes_are_covered(tickets: list[dict[str, Any]]) -> None:
    covered = {t["expected_route"] for t in tickets}
    assert covered == VALID_ROUTES, f"missing routes: {VALID_ROUTES - covered}"


def test_every_text_based_hard_trigger_reason_is_covered(tickets: list[dict[str, Any]]) -> None:
    """billing / human_request / unknown_case / out_of_procedure are each
    directly representable as an ``expected_reasons`` value."""
    covered_reasons = {r for t in tickets for r in t["expected_reasons"]}
    missing = HARD_TRIGGER_REASON_NAMES - covered_reasons
    assert not missing, f"hard-trigger reasons never used in any ticket: {missing}"


def test_every_low_confidence_subtype_is_covered(tickets: list[dict[str, Any]]) -> None:
    """DESIGN's Reason literal collapses empty-retrieval, verifier-failure,
    and classifier-abstention into a single "low_confidence" value (see
    backend/src/escalation/rules.py's module docstring), so coverage of
    each of the three underlying hard triggers can only be checked via the
    id-naming convention documented in labeled_set.yaml's
    meta.id_convention."""
    low_confidence_ids = {
        t["id"] for t in tickets if "low_confidence" in t["expected_reasons"]
    }
    for marker in LOW_CONFIDENCE_SUBTYPE_ID_MARKERS:
        matching = {i for i in low_confidence_ids if marker in i}
        assert matching, (
            f"no low_confidence-reasoned ticket id contains {marker!r} — "
            "docs/tickets.json T-7 requires every hard trigger covered, and DESIGN "
            "collapses empty retrieval / verifier failure / classifier abstention "
            "into the same reason value, so this is the only way to check each "
            "is independently represented"
        )


def test_frustration_and_complexity_have_both_clear_and_borderline_examples(
    tickets: list[dict[str, Any]],
) -> None:
    """docs/tickets.json T-7 acceptance: "fuzzy frustration/complexity cases
    that are genuinely borderline" — checked via the id-naming convention
    (```-borderline-``` vs a plain clear-cut id) documented in
    labeled_set.yaml's meta.id_convention."""
    for category in ("frustration", "complexity"):
        ids = [t["id"] for t in tickets if category in t["id"]]
        borderline = [i for i in ids if "borderline" in i]
        clear = [i for i in ids if "borderline" not in i]
        assert borderline, f"no borderline {category} examples found"
        assert clear, f"no clear-cut {category} examples found"


def test_adversarial_near_miss_examples_exist_and_do_not_escalate(
    tickets: list[dict[str, Any]],
) -> None:
    """docs/tickets.json T-7 acceptance: "adversarial phrasing" — a ticket
    that merely mentions billing/invoice language or the word "human"
    without genuinely tripping a hard trigger must NOT be labeled
    escalate=True."""
    adversarial = [t for t in tickets if "adversarial" in t["id"] and "esc-" not in t["id"]]
    assert adversarial, "no non-escalating adversarial near-miss tickets found"
    for t in adversarial:
        assert t["expected_escalate"] is False, t["id"]


def test_adversarial_polite_but_should_escalate_examples_exist(
    tickets: list[dict[str, Any]],
) -> None:
    """docs/tickets.json T-7 acceptance: "a polite ticket that should
    [escalate]" — tone must not suppress a genuine hard trigger."""
    polite_escalating = [t for t in tickets if "polite" in t["id"]]
    assert polite_escalating, "no polite-but-should-escalate adversarial tickets found"
    for t in polite_escalating:
        assert t["expected_escalate"] is True, t["id"]


# -- the human-approval gate itself -------------------------------------------


def test_approval_header_exists_with_the_pinned_shape(raw_document: dict[str, Any]) -> None:
    approval = raw_document.get("approval")
    assert approval is not None, "evals/labeled_set.yaml is missing its approval: header"
    for key in ("status", "approved_by", "approved_date"):
        assert key in approval, f"approval block missing {key!r}"


def test_approval_is_attributed_to_a_named_human_with_a_date(
    raw_document: dict[str, Any],
) -> None:
    """The core safety property this ticket exists to protect: the coding
    agent that authored these labels must never mark them approved.
    docs/tickets.json T-7 non-goal: "no synthetic label approval — the human
    sign-off is external ground truth."

    This previously asserted `status != APPROVED`, and its own docstring
    anticipated this moment: "If this test ever fails because status
    legitimately became APPROVED, that must only be because a human edited
    the file themselves after reading evals/REVIEW.md." That is what
    happened — the project owner signed off by hand on 2026-08-15.

    So the property is re-expressed rather than dropped. "Not approved" was
    only ever a proxy for "not approved BY A MACHINE"; now that a human has
    signed, the test enforces the real invariant: an approval must carry an
    attributable human name and a date. An unattributed or agent-shaped
    sign-off is exactly the synthetic approval the non-goal forbids, and
    still fails here.
    """
    approval = raw_document["approval"]
    if approval["status"] != "APPROVED":
        return  # still awaiting review — nothing to attribute yet

    approved_by = str(approval.get("approved_by") or "").strip()
    approved_date = str(approval.get("approved_date") or "").strip()

    assert approved_by, (
        "approval.status is APPROVED with no approved_by — an unattributed "
        "sign-off is indistinguishable from a synthetic one"
    )
    assert approved_date, "approval.status is APPROVED with no approved_date"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", approved_date), (
        f"approved_date {approved_date!r} is not an ISO yyyy-mm-dd date"
    )
    agentish = ("claude", "gpt", "agent", "assistant", "bot", "automation", "ci")
    assert not any(token in approved_by.lower() for token in agentish), (
        f"approved_by {approved_by!r} looks like an automated signer — the human "
        "sign-off is external ground truth and may never be machine-authored"
    )
