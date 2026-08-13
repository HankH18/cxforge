"""Graph node functions — DESIGN §Agent graph's pinned pipeline:

    ingest -> classify -> route -> {case_status | permission | kb_answer |
    off_topic} -> compose -> verify -> decide -> act

Every node has the signature ``(state: RunState, config: RunnableConfig) ->
dict[str, Any]`` (a partial state update LangGraph merges in) — see
``agent.graph.build_graph`` for how ``config["configurable"]`` carries the
run's ``ticket_id`` and its ``AgentDeps`` (port/llm/escalation_decider).

R9 boundary, concretely, in this file: ``case_status`` and ``permission``
only ever put a *tool result* (a ``data.Case``, an ``AlwaysGrantKind``)
into ``tool_results``. Only ``compose`` turns that into reply text, and
only via ``agent.templates``'s template-fill functions — never via an LLM
call. The ``"kb"`` route is the sole exception DESIGN allows (free
generation over retrieved KB content), and it is immediately gated by
``verify`` before ``decide``/``act`` can send it — by BOTH an LLM
groundedness score AND, independently, ``agent.grounding_guard``'s
pure-Python, no-LLM check that the free-generated draft asserts no
case-fact-shaped claim (an id, a stage, an ETA, a DNA/photo-availability
statement) the judge's score alone could not be trusted to catch, since the
judge is the same ``LLMClient`` instance that wrote the draft it is
scoring. See ``verify``'s and ``agent.grounding_guard``'s own docstrings.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig

from agent import store, templates
from agent.config import (
    DEFAULT_ESCALATION_GROUP_ID,
    DEFAULT_ESCALATION_GROUP_NAME,
    RETRIEVAL_K,
    VERIFIER_THRESHOLD,
)
from agent.escalation_seam import EscalationDecider, EscalationTrigger
from agent.grounding_guard import GuardViolation, find_ungrounded_case_claims
from agent.llm import LLMClient
from agent.prompts import (
    CLASSIFY_SYSTEM,
    GROUNDEDNESS_JUDGE_SYSTEM,
    KB_ANSWER_SYSTEM,
    PERMISSION_SYSTEM,
)
from agent.schemas import Classification, GroundednessJudgment, KBAnswerDraft, PermissionMatch
from agent.state import RunState
from data import Case, RetrievedChunk, get_case, get_cases_by_requester, search_kb
from escalation.notes import compose_internal_note
from escalation.schemas import Reason
from helpdesk.models import EscalationGroup, Message, Ticket
from helpdesk.port import HelpdeskPort

# -- shared helpers -----------------------------------------------------


class AgentDeps:
    """Per-deployment collaborators every node reaches through
    ``config["configurable"]["deps"]`` — never module-level globals, so a
    graph run in a test never touches real Postgres-adjacent-only-in-name
    singletons or a real port by accident."""

    def __init__(
        self, *, port: HelpdeskPort, llm: LLMClient, escalation_decider: EscalationDecider
    ) -> None:
        self.port = port
        self.llm = llm
        self.escalation_decider = escalation_decider


def _deps(config: RunnableConfig) -> AgentDeps:
    return config["configurable"]["deps"]


def _ticket_id(config: RunnableConfig) -> str:
    return config["configurable"]["ticket_id"]


def _actions(state: RunState, *new: str) -> list[str]:
    return [*state.get("actions", []), *new]


def _tool_results(state: RunState) -> dict[str, Any]:
    return dict(state.get("tool_results") or {})


def _latest_customer_message(conversation: list[Message]) -> str:
    for message in reversed(conversation):
        if message.author_kind == "customer":
            return message.text
    return ""


def _conversation_transcript(conversation: list[Message]) -> str:
    speaker_labels = {"customer": "Customer", "agent": "Agent", "ai": "AI"}
    lines = [f"{speaker_labels[m.author_kind]}: {m.text}" for m in conversation]
    return "\n".join(lines)


def _resolve_case(ticket: Ticket, case_id_hint: str | None) -> Case | None:
    """Resolve exactly the case this requester is asking about, or
    ``None`` if it cannot be resolved WITHOUT guessing (R2/R9: never
    invent). ``None`` covers three structurally distinct misses, all
    treated identically to the customer (no confirm/deny either way, per
    ``fixtures/kb/case-information-authorization.md``'s "Requests from
    someone not on the case"):

    - an explicit case_id that doesn't exist (``CaseNotFound``);
    - an explicit case_id that exists but is on file for a *different*
      requester than the one asking;
    - no case_id volunteered, and the requester's email maps to zero or
      more than one case (nothing to safely default to).
    """
    if case_id_hint:
        result = get_case(case_id_hint)
        if isinstance(result, Case) and result.requester_email == ticket.requester_email:
            return result
        return None
    candidates = get_cases_by_requester(ticket.requester_email)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _escalate_for(
    config: RunnableConfig,
    state: RunState,
    trigger: EscalationTrigger,
    *,
    tool_results: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Forward a T-5-detected condition to the escalation seam. Callers
    that already computed an updated ``tool_results`` this node-call (e.g.
    ``permission`` folding in the resolved case before escalating) pass it
    explicitly so the escalation decision — and later, ``act``'s internal
    note — sees it; callers with nothing new leave it to default to the
    state's own."""
    deps = _deps(config)
    resolved_tool_results = tool_results if tool_results is not None else _tool_results(state)
    decision = deps.escalation_decider.decide(
        trigger=trigger,
        ticket=state["ticket"],
        conversation=state["conversation"],
        topic=state.get("topic", ""),
        tool_results=resolved_tool_results,
    )
    return {"route": "escalate", "escalation": decision, "tool_results": resolved_tool_results}


# -- ingest ---------------------------------------------------------------


def ingest(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    """R7: rebuild ticket + conversation from the live port every run —
    never trust anything the caller passed in, and never persist context
    across runs."""
    deps = _deps(config)
    ticket_id = _ticket_id(config)
    ticket = deps.port.fetch_ticket(ticket_id)
    conversation = deps.port.fetch_conversation(ticket_id)
    return {
        "ticket": ticket,
        "conversation": conversation,
        "tool_results": {},
        "retrieved_chunks": [],
        "draft": None,
        "verifier_score": None,
        "escalation": None,
        "actions": ["ingest"],
    }


# -- classify ---------------------------------------------------------------


def classify(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    """The only node allowed to choose among the four branch routes —
    never ``"escalate"`` itself (see ``agent.state``'s module docstring)."""
    deps = _deps(config)
    transcript = _conversation_transcript(state["conversation"])
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": CLASSIFY_SYSTEM},
        {"role": "user", "content": transcript or _latest_customer_message(state["conversation"])},
    ]
    result = deps.llm.structured(Classification, messages)
    assert isinstance(result, Classification)

    tool_results = _tool_results(state)
    tool_results["case_id_hint"] = result.case_id

    return {
        "topic": result.topic,
        "route": result.route,
        "confidence": result.confidence,
        "tool_results": tool_results,
        "actions": _actions(state, "classify"),
    }


# -- route (pure dispatch node; see agent.graph for the conditional edge) --


def route_dispatch(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    """A no-op node purely to give the pinned pipeline a distinct ``route``
    step to hang ``add_conditional_edges`` off of — ``classify`` already
    decided ``state["route"]``."""
    return {}


# -- case_status (R2) --------------------------------------------------


def case_status(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    ticket = state["ticket"]
    case_id_hint = _tool_results(state).get("case_id_hint")
    case = _resolve_case(ticket, case_id_hint)
    actions = _actions(state, "case_status")

    if case is None:
        detail = (
            f"Could not resolve a case for requester {ticket.requester_email}"
            + (
                f" (message referenced case_id={case_id_hint!r}, which either doesn't "
                "exist or isn't on file for this requester)"
                if case_id_hint
                else " (no case_id given, and the requester's email matched zero or "
                "more than one case on file)"
            )
        )
        update = _escalate_for(
            config, state, EscalationTrigger(reason="unknown_case", detail=detail)
        )
        return {**update, "actions": actions}

    tool_results = _tool_results(state)
    tool_results["case"] = case
    return {"tool_results": tool_results, "actions": actions}


# -- permission (R3) -----------------------------------------------------


def permission(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    ticket = state["ticket"]
    case_id_hint = _tool_results(state).get("case_id_hint")
    case = _resolve_case(ticket, case_id_hint)
    actions = _actions(state, "permission")

    if case is None:
        detail = (
            f"Could not resolve a case for requester {ticket.requester_email} "
            "to check permission against"
        )
        update = _escalate_for(
            config, state, EscalationTrigger(reason="unknown_case", detail=detail)
        )
        return {**update, "actions": actions}

    # Ground the always-grant match in the KB's own policy text (R3:
    # "grounded in the KB's always-grant list, don't invent a new one") —
    # never let the model classify against its own memory of the policy.
    policy_chunks = search_kb(
        "always granted permission request authorized contact resend report "
        "extend retention window",
        k=RETRIEVAL_K,
    )
    policy_text = "\n\n".join(chunk.chunk.text for chunk in policy_chunks)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": PERMISSION_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Policy text:\n{policy_text}\n\n"
                f"Customer request:\n{_latest_customer_message(state['conversation'])}"
            ),
        },
    ]
    result = deps.llm.structured(PermissionMatch, messages)
    assert isinstance(result, PermissionMatch)

    tool_results = _tool_results(state)
    tool_results["case"] = case
    tool_results["retrieved_policy_chunks"] = policy_chunks

    if result.kind is None:
        update = _escalate_for(
            config,
            state,
            EscalationTrigger(
                reason="out_of_procedure",
                detail="Permission request did not match any always-granted kind",
            ),
            tool_results=tool_results,
        )
        return {**update, "actions": actions}

    tool_results["permission_kind"] = result.kind
    return {"tool_results": tool_results, "actions": actions}


# -- kb_answer (R4, grounding step only — compose does the generation) ---


def kb_answer(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    query = state.get("topic") or _latest_customer_message(state["conversation"])
    chunks = search_kb(query, k=RETRIEVAL_K)
    actions = _actions(state, "kb_answer")

    if not chunks:
        update = _escalate_for(
            config,
            state,
            EscalationTrigger(
                reason="low_confidence", detail="Empty KB retrieval for this question"
            ),
        )
        return {**update, "retrieved_chunks": [], "actions": actions}

    return {"retrieved_chunks": chunks, "actions": actions}


# -- off_topic (R5) -------------------------------------------------------


def off_topic(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    """Nothing to ground — ``compose`` fills the fixed redirect copy."""
    return {"actions": _actions(state, "off_topic")}


# -- compose --------------------------------------------------------------


def compose(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    """The ONLY node that writes ``state["draft"]``. Case facts reach it
    exclusively via ``agent.templates.render_case_status_reply(case)`` —
    the ``case`` argument is a tool result already sitting in
    ``tool_results["case"]``, put there by ``case_status``/``permission``
    calling ``data.get_case``/``data.get_cases_by_requester`` earlier in
    THIS run. Free generation (an LLM call) happens only for
    ``route == "kb"``, per DESIGN."""
    route = state["route"]
    actions = _actions(state, "compose")
    tool_results = state.get("tool_results") or {}

    if route == "case_status":
        case = tool_results["case"]
        assert isinstance(case, Case)
        draft = templates.render_case_status_reply(case)
    elif route == "permission":
        kind = tool_results["permission_kind"]
        draft = templates.render_permission_grant_reply(kind)
    elif route == "kb":
        deps = _deps(config)
        chunks = state.get("retrieved_chunks") or []
        context = "\n\n".join(
            f"[{c.chunk.doc_slug}#{c.chunk.chunk_index}] {c.chunk.text}" for c in chunks
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": KB_ANSWER_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Knowledge base context:\n{context}\n\n"
                    f"Customer question ({state.get('topic', '')}):\n"
                    f"{_latest_customer_message(state['conversation'])}"
                ),
            },
        ]
        result = deps.llm.structured(KBAnswerDraft, messages)
        assert isinstance(result, KBAnswerDraft)
        draft = result.answer
    elif route == "off_topic":
        draft = templates.OFF_TOPIC_REPLY
    elif route == "escalate":
        # A branch already forwarded a T-5-detected condition to the
        # escalation seam before compose ran (e.g. case_status's
        # unresolvable case) — the customer-facing copy is fixed, never
        # generated, so there is nothing left to compose from a draft that
        # was never grounded in the first place.
        draft = templates.ESCALATION_CUSTOMER_REPLY
    else:  # pragma: no cover - Route is exhaustively handled above
        raise ValueError(f"unhandled route in compose: {route!r}")

    return {"draft": draft, "actions": actions}


# -- verify (R4's groundedness gate) --------------------------------------


def _score_groundedness(llm: LLMClient, draft: str, chunks: list[RetrievedChunk]) -> float:
    context = "\n\n".join(c.chunk.text for c in chunks)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": GROUNDEDNESS_JUDGE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"KB context:\n{context}\n\nDraft answer:\n{draft}\n\n"
                "Score how fully the draft is supported by the context."
            ),
        },
    ]
    result = llm.structured(GroundednessJudgment, messages)
    assert isinstance(result, GroundednessJudgment)
    return max(0.0, min(1.0, result.score))


def _guard_violation_detail(violations: list[GuardViolation], score: float) -> str:
    shapes = "; ".join(v.detail for v in violations)
    return (
        f"Deterministic grounding guard blocked free-generated text: {shapes} "
        f"(LLM groundedness judge scored this draft {score:.2f} — the guard runs "
        "independently of that score and a high score cannot override it)"
    )


def verify(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    """Scores KB drafts for groundedness against retrieved chunks, AND runs
    ``agent.grounding_guard.find_ungrounded_case_claims`` — a pure-Python,
    no-LLM check for R9 (see that module's docstring for the full
    rationale: the groundedness judge is an ``LLMClient`` call, the same
    kind of call that wrote the draft, so a hostile/broken model can
    fabricate a case fact and self-score it 1.0; the guard is the
    judge-independent backstop that can't be bought off by a high score).

    Both checks can independently force ``route`` to ``"escalate"`` and
    discard the unverified draft (R4: "otherwise escalate" — never send a
    draft that failed verification). The guard runs and is checked
    UNCONDITIONALLY, before the score-threshold branch and regardless of
    what the score was — a 1.0 groundedness score does not skip it and
    cannot suppress an escalation it decides on. Every other route has
    nothing to verify (templated/closed-list/fixed copy) and no
    free-generated text to guard, so ``verifier_score`` stays ``None`` and
    the guard never runs for them."""
    actions = _actions(state, "verify")
    if state["route"] != "kb":
        return {"verifier_score": None, "actions": actions}

    deps = _deps(config)
    draft = state.get("draft") or ""
    score = _score_groundedness(deps.llm, draft, state.get("retrieved_chunks") or [])

    violations = find_ungrounded_case_claims(draft, _tool_results(state))
    if violations:
        update = _escalate_for(
            config,
            state,
            EscalationTrigger(
                reason="low_confidence",
                detail=_guard_violation_detail(violations, score),
            ),
        )
        return {
            **update,
            "verifier_score": score,
            "draft": templates.ESCALATION_CUSTOMER_REPLY,
            "actions": actions,
        }

    if score < VERIFIER_THRESHOLD:
        update = _escalate_for(
            config,
            state,
            EscalationTrigger(
                reason="low_confidence",
                detail=f"Groundedness score {score:.2f} below threshold {VERIFIER_THRESHOLD}",
            ),
        )
        return {
            **update,
            "verifier_score": score,
            "draft": templates.ESCALATION_CUSTOMER_REPLY,
            "actions": actions,
        }

    return {"verifier_score": score, "actions": actions}


# -- decide (R11's gate) ---------------------------------------------------


def decide(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    """Reads R11's gate and stashes the decision for ``act`` — the actual
    port calls / persistence live in ``act``, so ``decide`` never touches
    the port or the database's write tables directly.

    Also runs DESIGN's full escalation combinator — ``EscalationDecider.
    evaluate`` (``escalation.engine.EscalationEngine`` in production): hard
    rules first (billing terms, explicit human request — pure, LLM-free,
    over the conversation's latest customer message), then, only if none
    fired, the classifier (frustration/complexity, one ``LLMClient`` call)
    — for any run not already routed to ``"escalate"`` by an earlier node's
    own structural detection (unknown_case/out_of_procedure/empty-retrieval/
    verifier-threshold; see ``agent.escalation_seam``'s module docstring).
    Calling the single ``evaluate`` combinator here, rather than
    reimplementing its hard-rule-then-classifier logic inline, is what
    guarantees the ordering DESIGN pins (a fired hard rule always
    short-circuits before the classifier is ever consulted) — the same
    guarantee ``backend/tests/escalation/test_combinator.py`` and
    ``test_adversarial.py`` already prove against the engine directly.

    This runs UNCONDITIONALLY on every route that reaches this point still
    un-escalated — case_status, permission, kb, and off_topic alike — not
    only "kb". DESIGN's frustration/complexity signal is about the
    customer's own conversation, not about which route ``classify`` picked;
    a furious customer asking a routine case-status question must still be
    escalatable (see ``backend/tests/graph/test_live_escalation_classifier.
    py``). The ONLY skip is ``state["route"] == "escalate"`` already: an
    earlier node found a hard-rule-equivalent structural condition, DESIGN's
    combinator is an OR (nothing changes an already-True decision), and the
    customer-facing reply is already the fixed
    ``templates.ESCALATION_CUSTOMER_REPLY`` — there is nothing left to
    judge, and calling the classifier there would be a wasted ``LLMClient``
    call with no possible effect on the outcome."""
    deps = _deps(config)
    gate_enabled = store.read_gate_enabled()
    tool_results = _tool_results(state)
    tool_results["decision"] = {"gate_enabled": gate_enabled}
    actions = _actions(state, "decide")

    if state["route"] != "escalate":
        decision = deps.escalation_decider.evaluate(
            ticket=state["ticket"],
            conversation=state["conversation"],
            topic=state.get("topic", ""),
            tool_results=tool_results,
        )
        if decision.escalate:
            return {
                "route": "escalate",
                "escalation": decision,
                "tool_results": tool_results,
                "draft": templates.ESCALATION_CUSTOMER_REPLY,
                "actions": actions,
            }

    return {"tool_results": tool_results, "actions": actions}


# -- act --------------------------------------------------------------------

_OUTCOME_BY_ROUTE = {
    "case_status": "auto_sent",
    "permission": "auto_sent",
    "kb": "auto_sent",
    "off_topic": "off_topic",
    "escalate": "escalated",
}


def _escalation_reasons(state: RunState) -> list[Reason]:
    """The escalation decision's own reasons, for ``store.record_run``'s
    ``reasons`` column (SPEC R13 / DESIGN's ``escalations_by_reason``) — the
    exact ``EscalationTrigger.reason`` list DESIGN's combinator attached to
    THIS run, never re-derived or guessed here. ``[]`` for a run that never
    escalated (``state["escalation"]`` is only ever set once a branch node
    or ``decide`` has forwarded a condition to the escalation seam — see
    ``RunState.escalation``'s own docstring), which ``record_run`` stores as
    an empty array, not a fake reason."""
    decision = state.get("escalation")
    if decision is None:
        return []
    return [trigger.reason for trigger in decision.triggers]


def act(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    """Performs the HelpdeskPort calls (gate OFF) or persists the draft as
    ``pending`` (gate ON) and records the run. Gate ON never calls the port
    at all — R11: "ON: every outbound public reply is held as a draft for
    reviewer edit/approve/reject before send"; T-8's approval flow is what
    eventually calls the port for a gated draft.

    The ``"escalate"`` branch's port-call order follows SPEC R6's own
    wording exactly: "post an internal note ..., tag, assign to the
    escalation group, and publicly tell the customer" — internal
    housekeeping first, the customer-facing notice last. Every other route
    keeps its original order (public reply, then tags/status) unchanged."""
    deps = _deps(config)
    ticket_id = _ticket_id(config)
    route = state["route"]
    assert route is not None, "act reached with no route set — classify/branch invariant broken"
    draft = state.get("draft") or ""
    tool_results = state.get("tool_results") or {}
    gate_enabled = bool(tool_results.get("decision", {}).get("gate_enabled", False))
    actions = _actions(state, "act")

    received_at = datetime.now(UTC)
    trace_id = uuid.uuid4().hex

    if gate_enabled:
        run_id = store.record_run(
            ticket_id=ticket_id,
            route=route,
            confidence=state.get("confidence"),
            outcome=None,
            verifier_score=state.get("verifier_score"),
            trace_id=trace_id,
            received_at=received_at,
            replied_at=None,
            reasons=_escalation_reasons(state),
        )
        store.record_draft(run_id=run_id, body=draft, status="pending")
        return {"actions": [*actions, "gate:held_pending"]}

    if route == "escalate":
        decision = state.get("escalation")
        triggers = decision.triggers if decision is not None else []
        note_body = compose_internal_note(
            topic=state.get("topic") or "(no summary available)",
            tool_results=tool_results,
            triggers=triggers,
            retrieved_chunks=state.get("retrieved_chunks"),
        )
        deps.port.post_internal_note(ticket_id, note_body)
        deps.port.add_tags(ticket_id, ["escalated", *[t.reason for t in triggers]])
        deps.port.assign_group(
            ticket_id,
            EscalationGroup(
                group_id=DEFAULT_ESCALATION_GROUP_ID, name=DEFAULT_ESCALATION_GROUP_NAME
            ),
        )
        deps.port.set_status(ticket_id, "open")
        deps.port.post_public_reply(ticket_id, draft)
        actions += [
            "port:post_internal_note",
            "port:add_tags",
            "port:assign_group",
            "port:set_status:open",
            "port:post_public_reply",
        ]
    else:
        deps.port.post_public_reply(ticket_id, draft)
        actions.append("port:post_public_reply")

        if route in ("case_status", "permission", "kb"):
            tag = {
                "case_status": "case-status",
                "permission": "permission-granted",
                "kb": "kb-answer",
            }[route]
            deps.port.add_tags(ticket_id, [tag])
            deps.port.set_status(ticket_id, "solved")
            actions += ["port:add_tags", "port:set_status:solved"]
        elif route == "off_topic":
            deps.port.add_tags(ticket_id, ["off-topic"])
            deps.port.set_status(ticket_id, "open")
            actions += ["port:add_tags", "port:set_status:open"]

    outcome = _OUTCOME_BY_ROUTE[route]
    run_id = store.record_run(
        ticket_id=ticket_id,
        route=route,
        confidence=state.get("confidence"),
        outcome=outcome,
        verifier_score=state.get("verifier_score"),
        trace_id=trace_id,
        received_at=received_at,
        replied_at=datetime.now(UTC),
        reasons=_escalation_reasons(state),
    )
    store.record_draft(run_id=run_id, body=draft, status="auto_sent")

    return {"actions": actions}
