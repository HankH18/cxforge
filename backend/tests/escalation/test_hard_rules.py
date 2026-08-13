"""Each of DESIGN §Escalation contract's seven hard rules, individually —
one predicate per rule (``escalation.rules``), each tested both firing and
not firing. No LLM, no DB: every predicate here is pure Python.
"""

from __future__ import annotations

from escalation.rules import (
    is_billing_dispute,
    is_classifier_abstention,
    is_explicit_human_request,
    is_low_confidence_trigger,
    is_out_of_procedure,
    is_unknown_case,
)
from escalation.schemas import EscalationCall

# -- billing dispute -----------------------------------------------------


def test_billing_dispute_fires_on_duplicate_charge() -> None:
    assert is_billing_dispute("I think I got charged twice for the extraction fee.")


def test_billing_dispute_fires_on_explicit_dispute_language() -> None:
    assert is_billing_dispute("I want to dispute this charge on my last invoice.")


def test_billing_dispute_fires_on_refund_demand() -> None:
    assert is_billing_dispute("Please issue a refund for the sequencing fee, it's wrong.")


def test_billing_dispute_does_not_fire_on_routine_billing_question() -> None:
    """fixtures/kb/billing-and-payment-terms.md: "straightforward,
    non-disputed billing questions are fine to answer" — the rule must not
    treat ordinary billing vocabulary as a dispute."""
    assert not is_billing_dispute("How does billing work, and when do I get billed?")


def test_billing_dispute_does_not_fire_on_refund_eligibility_question() -> None:
    """fixtures/kb/refund-policy.md: eligibility questions are answerable
    directly; only a dispute or an adjustment DEMAND hard-escalates."""
    assert not is_billing_dispute("Is the intake fee refundable if I withdraw my case?")


def test_billing_dispute_does_not_fire_on_unrelated_message() -> None:
    assert not is_billing_dispute("What's the status of my case?")


# -- explicit human request -----------------------------------------------


def test_human_request_fires_on_talk_to_a_person() -> None:
    assert is_explicit_human_request("Can I talk to a real person about this, not a bot?")


def test_human_request_fires_on_escalate_to_human() -> None:
    assert is_explicit_human_request("Please escalate this to a human right away.")


def test_human_request_fires_on_this_is_a_bot() -> None:
    assert is_explicit_human_request("I know this is a bot, I need a person to help me.")


def test_human_request_does_not_fire_on_unrelated_message() -> None:
    assert not is_explicit_human_request("How long does sequencing usually take?")


def test_human_request_does_not_fire_on_the_word_agent_alone() -> None:
    assert not is_explicit_human_request("Is my case being worked on by your lab right now?")


# -- unknown/unresolvable case (already detected upstream by T-5) ---------


def test_unknown_case_fires_when_upstream_reason_matches() -> None:
    assert is_unknown_case("unknown_case")


def test_unknown_case_does_not_fire_for_other_reasons() -> None:
    assert not is_unknown_case("out_of_procedure")
    assert not is_unknown_case("low_confidence")
    assert not is_unknown_case(None)


# -- out-of-procedure request (already detected upstream by T-5) ----------


def test_out_of_procedure_fires_when_upstream_reason_matches() -> None:
    assert is_out_of_procedure("out_of_procedure")


def test_out_of_procedure_does_not_fire_for_other_reasons() -> None:
    assert not is_out_of_procedure("unknown_case")
    assert not is_out_of_procedure(None)


# -- empty retrieval / verifier_score < 0.7 (both collapse to
# "low_confidence" upstream — see escalation.rules' module docstring) -----


def test_low_confidence_trigger_fires_for_empty_retrieval_shaped_trigger() -> None:
    # agent.nodes.kb_answer forwards an empty-retrieval condition under
    # reason="low_confidence" — this predicate only looks at the reason.
    assert is_low_confidence_trigger("low_confidence")


def test_low_confidence_trigger_fires_for_verifier_failure_shaped_trigger() -> None:
    # agent.nodes.verify forwards a groundedness-score failure under the
    # SAME reason — DESIGN's Reason literal has no separate value for it.
    assert is_low_confidence_trigger("low_confidence")


def test_low_confidence_trigger_does_not_fire_for_other_reasons() -> None:
    assert not is_low_confidence_trigger("unknown_case")
    assert not is_low_confidence_trigger(None)


# -- classifier abstention --------------------------------------------------


def test_classifier_abstention_fires_on_none() -> None:
    assert is_classifier_abstention(None)


def test_classifier_abstention_does_not_fire_on_a_real_call() -> None:
    call = EscalationCall(escalate=False, reasons=[], confidence=0.9)
    assert not is_classifier_abstention(call)
