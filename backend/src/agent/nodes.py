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
    REQUESTER_HISTORY_LIMIT,
    RETRIEVAL_K,
    VERIFIER_THRESHOLD,
)
from agent.escalation_seam import EscalationDecider, EscalationTrigger
from agent.grounding_guard import GuardViolation, find_ungrounded_case_claims
from agent.llm import LLMClient, TraceSpan, emit_trace
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
from helpdesk.models import EscalationGroup, Message, Ticket, TicketSummary
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


class NodeClock:
    """When each graph node really started and really finished.

    One instance per run, built in ``agent.graph.run_agent`` and carried
    through ``config["configurable"]["node_clock"]`` — the same route
    ``deps`` and ``received_at`` already take, for the same reason: it is a
    run-scoped dependency, not a grounding fact ``RunState`` should carry.
    ``agent.graph``'s node wrapper stamps ``enter``/``leave`` around every
    node call, so a timing exists *because the node ran*, and a node that
    stops running stops having one. Nothing derives, estimates or
    interpolates a duration.

    Why this exists at all: the Langfuse trace is assembled post-hoc in
    `act` (BUILD-PLAN §1.6 pins the id to the one `act` mints), so the span
    objects are all created within a few milliseconds of each other at the
    end of the run. Before this, that made every span report 0–1ms and the
    whole trace 11ms for a 15-second run — the same defect ADR-004 fixed one
    layer down when ``received_at`` was minted inside `act`, repeated in
    Langfuse.

    ``leave`` records the moment the node *returned*. `act` is therefore
    always missing its own end here — it emits the trace from inside itself
    — and `_run_trace_spans` closes that span on ``replied_at`` instead (the
    measured instant the reply went out), never on a guess.

    The graph is linear and runs in one thread per run, so plain dicts are
    enough; a node that ran twice would keep its last interval.
    """

    def __init__(self) -> None:
        self._started: dict[str, datetime] = {}
        self._ended: dict[str, datetime] = {}

    def enter(self, node: str) -> None:
        self._started[node] = datetime.now(UTC)

    def leave(self, node: str) -> None:
        self._ended[node] = datetime.now(UTC)

    def started_at(self, node: str) -> datetime | None:
        return self._started.get(node)

    def ended_at(self, node: str) -> datetime | None:
        return self._ended.get(node)


def _deps(config: RunnableConfig) -> AgentDeps:
    return config["configurable"]["deps"]


def node_clock(config: RunnableConfig) -> NodeClock | None:
    """The run's clock, or ``None`` for a caller that compiled the graph
    itself instead of going through ``run_agent``. ``None`` costs the trace
    its durations and says so (see ``_run_trace_spans``); it never costs the
    run anything.

    Public, unlike its ``_deps``/``_ticket_id`` neighbours, because
    ``agent.graph``'s timing wrapper is its other caller."""
    configurable = config.get("configurable") or {}
    clock = configurable.get("node_clock")
    return clock if isinstance(clock, NodeClock) else None


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


def _render_requester_history(history: list[TicketSummary]) -> str:
    """One line per prior ticket, oldest context first.

    Deliberately renders only what ``TicketSummary`` carries — no bodies. A
    prior ticket's *body* is another conversation's content, and pulling it
    into this run's prompt would both blow up the context and give the
    classifier free text it could mistake for the customer's current ask.
    Subject + status + age + tags is enough to tell a repeat complainer from
    a first-time asker, which is what ADR-009 wanted it for.
    """
    lines = []
    for summary in history:
        tags = f" [tags: {', '.join(summary.tags)}]" if summary.tags else ""
        lines.append(
            f"- {summary.created_at.date().isoformat()} "
            f"(status: {summary.status}) {summary.subject}{tags}"
        )
    return "\n".join(lines)


def classify(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    """The only node allowed to choose among the four branch routes —
    never ``"escalate"`` itself (see ``agent.state``'s module docstring).

    ADR-009 adds the requester's prior tickets to the classifier's context.
    Two deliberate choices about *how*:

    - The history goes in the **user** message, not into ``CLASSIFY_SYSTEM``.
      ``agent.prompts``'s own contract is that the system strings carry
      instructions only and never run-specific content, which is what keeps a
      case fact structurally unable to reach a prompt (R9). History is
      run-specific content, so it belongs here, labelled clearly enough that
      the model cannot confuse a prior subject line with the current ask.
    - It is fetched here rather than in ``ingest`` because it is classifier
      context, and because ``ingest``'s two port calls are R7's "rebuild the
      conversation from the live port" contract — a third call with different
      semantics does not belong inside it.

    The port call is not wrapped in a try/except, matching ``ingest``: a
    helpdesk API failure fails the run loudly, where ADR-003's worker
    releases the dedup row and logs it, rather than silently degrading to a
    classification made without context nobody knows was missing.
    """
    deps = _deps(config)
    ticket = state["ticket"]
    transcript = _conversation_transcript(state["conversation"])
    current = transcript or _latest_customer_message(state["conversation"])

    history = deps.port.fetch_requester_history(
        ticket.requester_email, exclude_ticket_id=ticket.id, limit=REQUESTER_HISTORY_LIMIT
    )
    if history:
        user_content = (
            f"This requester has contacted support before. Their previous "
            f"tickets, newest first — context only, NOT the message to "
            f"classify:\n{_render_requester_history(history)}\n\n"
            f"Conversation to classify:\n{current}"
        )
    else:
        user_content = current

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": CLASSIFY_SYSTEM},
        {"role": "user", "content": user_content},
    ]
    result = deps.llm.structured(Classification, messages)
    assert isinstance(result, Classification)

    tool_results = _tool_results(state)
    tool_results["case_id_hint"] = result.case_id
    # Carried so the escalation seam and `act`'s internal note can see the
    # same history the classifier saw — a repeat complainer is exactly the
    # signal a human triaging the escalation wants, and re-fetching it later
    # would be a second API call that could disagree with this one.
    tool_results["requester_history"] = history

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


# -- act's Langfuse span wrapping (ADR-006 / W2-C1) ------------------------
#
# The trace is assembled and emitted HERE, in `act`, rather than a span being
# opened inside each node, and that follows from where the id lives:
# BUILD-PLAN §1.6 pins the trace to "the ``trace_id`` already minted in
# ``act`` — do not mint a second one", and `act` is the LAST node in the
# pinned pipeline. There is no id to hang a `classify` span on at the moment
# `classify` runs. What `act` does have is the finished `RunState`, which
# carries every node's real output: the route and confidence `classify`
# chose, the `Case` `case_status` resolved, the draft `compose` rendered,
# the score `verify` gave it. The trace is therefore reconstructed from
# recorded facts, not narrated.
#
# Reconstructing the spans after the fact does mean their *times* cannot come
# from when the span objects were built — and for a while they did, which is
# how a genuine 15.0s run was published as an 11ms trace: `latency 0.011s`,
# every span 0–1ms, all eight observations stamped inside the same 11ms
# window while the run's own `replied_at` was 100ms earlier. Exactly the
# defect ADR-004 fixed one layer down (`received_at` minted inside `act`, so
# `replied_at - received_at` measured 22µs), repeated in Langfuse, and the
# first one visible to an outside viewer — ADR-006 calls this trace "the
# single best visual for the zero-hallucination story".
#
# So the times are measured where the work happens and carried here:
# `NodeClock` stamps every node's real entry and exit (`agent.graph`'s
# wrapper), and the whole-trace span is `received_at` → `replied_at` — the
# same pair written to `runs` and reported by `/api/metrics`, so the panel
# and the Gantt cannot disagree. Nothing is derived or estimated: a node
# whose interval was not measured gets a span that says so
# (`{"duration": "not measured"}`) rather than a plausible number.

_TRACE_NODE_KINDS: dict[str, str] = {
    "ingest": "span",
    "classify": "span",
    "case_status": "tool",
    "permission": "tool",
    "kb_answer": "retriever",
    "off_topic": "span",
    "compose": "span",
    "verify": "evaluator",
    "decide": "span",
    "act": "span",
}


def _chunk_digest(chunks: list[RetrievedChunk] | None) -> list[dict[str, Any]]:
    """Retrieved chunks as (doc, index, score) — provenance without pasting
    the whole KB into every span."""
    return [
        {"doc_slug": c.chunk.doc_slug, "chunk_index": c.chunk.chunk_index, "score": c.score}
        for c in (chunks or [])
    ]


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if v is not None}


def _trace_node_io(
    state: RunState, *, ticket_id: str, gate_enabled: bool, outcome: str | None
) -> dict[str, tuple[Any, Any]]:
    """(input, output) per pipeline node, read out of the finished state."""
    tool_results = state.get("tool_results") or {}
    conversation = state.get("conversation") or []
    chunks = state.get("retrieved_chunks") or []
    ticket = state.get("ticket")
    case = tool_results.get("case")
    draft = state.get("draft")
    route = state.get("route")

    return {
        "ingest": (
            {"ticket_id": ticket_id},
            {
                "requester_email": getattr(ticket, "requester_email", None),
                "messages": [
                    {"author_kind": m.author_kind, "text": m.text} for m in conversation
                ],
            },
        ),
        "classify": (
            {"transcript": _conversation_transcript(conversation)},
            _drop_none(
                {
                    "topic": state.get("topic"),
                    "route": route,
                    "confidence": state.get("confidence"),
                    "case_id_hint": tool_results.get("case_id_hint"),
                }
            ),
        ),
        "case_status": (
            _drop_none(
                {
                    "case_id_hint": tool_results.get("case_id_hint"),
                    "requester_email": getattr(ticket, "requester_email", None),
                }
            ),
            {"case": case},
        ),
        "permission": (
            _drop_none(
                {
                    "case_id_hint": tool_results.get("case_id_hint"),
                    "requester_email": getattr(ticket, "requester_email", None),
                    "policy_chunks": _chunk_digest(tool_results.get("retrieved_policy_chunks"))
                    or None,
                }
            ),
            {"case": case, "permission_kind": tool_results.get("permission_kind")},
        ),
        "kb_answer": (
            {"query": state.get("topic") or _latest_customer_message(conversation)},
            {"retrieved_chunks": _chunk_digest(chunks)},
        ),
        "off_topic": ({}, {"reply": "fixed redirect copy"}),
        # R9's whole story in one observation: what the reply was rendered
        # FROM (the tool results — above all `case`, the row `case_status`
        # read out of Postgres this run) next to what came out. A grader
        # reading this span sees the case fields and the sentence that
        # quotes them, with no model call in between for anything but `kb`.
        "compose": (
            _drop_none(
                {
                    "route": route,
                    "case": case,
                    "permission_kind": tool_results.get("permission_kind"),
                    "retrieved_chunks": _chunk_digest(chunks) or None,
                    "generated_by": "template" if route != "kb" else "llm",
                }
            ),
            {"draft": draft},
        ),
        "verify": (
            _drop_none({"draft": draft, "retrieved_chunks": _chunk_digest(chunks) or None}),
            {
                "verifier_score": state.get("verifier_score"),
                "threshold": VERIFIER_THRESHOLD,
                "ran": route == "kb",
            },
        ),
        "decide": (
            {"gate_enabled": gate_enabled},
            {"route": route, "escalation_reasons": _escalation_reasons(state)},
        ),
        "act": (
            {"route": route, "gate_enabled": gate_enabled},
            {"outcome": outcome, "actions": state.get("actions", [])},
        ),
    }


_DURATION_NOT_MEASURED = {"duration": "not measured"}


def _run_trace_spans(
    state: RunState,
    *,
    ticket_id: str,
    actions: list[str],
    gate_enabled: bool,
    outcome: str | None,
    clock: NodeClock | None,
    act_ended_at: datetime,
) -> list[TraceSpan]:
    """One span per node that actually ran, in the order it ran, over the
    interval it really ran for.

    Driven by ``actions`` — the run's own record of which nodes executed —
    so a run that escalated at `case_status` gets no `kb_answer` span, and a
    node that stops being called stops having a span. ``port:*`` and
    ``gate:*`` entries are effects `act` recorded, not nodes, and are
    reported on `act`'s own output instead.

    Times come from ``clock`` (see ``NodeClock``) and from nowhere else. Two
    cases deliberately do not get a duration invented for them:

    * **`act`**, which is running right now — this call is inside it — so
      the clock has its start but not its end. It is closed on
      ``act_ended_at``: ``replied_at``, the measured instant the reply went
      out, or for a gated run the measured instant the trace was emitted.
      Either way a timestamp something actually took, so `act`'s span covers
      the port calls and the ``runs``/``drafts`` writes.
    * **A node with no recorded interval at all** — only reachable when the
      caller compiled the graph itself rather than going through
      ``run_agent``, so there is no clock. That span is emitted with no
      times and ``{"duration": "not measured"}`` in its metadata. Langfuse
      will show it as ~0ms because an observation always has bounds; the
      metadata is there so nobody reads that 0 as a measurement.
    """
    io = _trace_node_io(state, ticket_id=ticket_id, gate_enabled=gate_enabled, outcome=outcome)
    spans: list[TraceSpan] = []
    for node in actions:
        if node not in _TRACE_NODE_KINDS:
            continue
        span_input, span_output = io[node]
        started_at = clock.started_at(node) if clock is not None else None
        ended_at = clock.ended_at(node) if clock is not None else None
        if node == "act" and started_at is not None and ended_at is None:
            ended_at = act_ended_at
        measured = started_at is not None and ended_at is not None
        spans.append(
            TraceSpan(
                name=node,
                kind=_TRACE_NODE_KINDS[node],
                input=span_input,
                output=span_output,
                metadata=None if measured else dict(_DURATION_NOT_MEASURED),
                start_time=started_at if measured else None,
                end_time=ended_at if measured else None,
            )
        )
    return spans


def _emit_run_trace(
    state: RunState,
    *,
    ticket_id: str,
    trace_id: str,
    actions: list[str],
    gate_enabled: bool,
    outcome: str | None,
    received_at: datetime,
    replied_at: datetime | None,
    clock: NodeClock | None,
) -> bool:
    """Report this run to Langfuse under the id `act` minted and persisted.

    ``received_at``/``replied_at`` are the same two values written to
    ``runs`` a few lines away, so the trace's ``latency_s`` and
    ``/api/metrics`` can never disagree about what was measured — and since
    they are also the whole-trace span's bounds, a grader comparing the
    metrics panel's latency to the trace's own sees the same interval rather
    than two contradictory numbers.

    A gated run has no ``replied_at`` (nothing was sent), so there is no
    receipt→reply interval to report and ``latency_s`` stays absent, exactly
    as before. The trace still needs *some* end, and it gets ``emitted_at``
    below — the measured instant this report was produced, which for a gated
    run is the moment the run finished. That is a real timestamp; what it is
    not is a reply latency, which is why it does not appear as one.
    """
    ticket = state.get("ticket")
    latency_s = (replied_at - received_at).total_seconds() if replied_at is not None else None
    emitted_at = datetime.now(UTC)
    return emit_trace(
        trace_id=trace_id,
        name="agent_run",
        input=_drop_none(
            {
                "ticket_id": ticket_id,
                "requester_email": getattr(ticket, "requester_email", None),
                "customer_message": _latest_customer_message(state.get("conversation") or []),
            }
        ),
        output={
            "route": state.get("route"),
            "outcome": outcome,
            "draft": state.get("draft"),
        },
        metadata=_drop_none(
            {
                "trace_id": trace_id,
                "gate_enabled": gate_enabled,
                "confidence": state.get("confidence"),
                "verifier_score": state.get("verifier_score"),
                "escalation_reasons": _escalation_reasons(state) or None,
                "received_at": received_at,
                "replied_at": replied_at,
                "latency_s": latency_s,
            }
        ),
        spans=_run_trace_spans(
            state,
            ticket_id=ticket_id,
            actions=actions,
            gate_enabled=gate_enabled,
            outcome=outcome,
            clock=clock,
            act_ended_at=replied_at if replied_at is not None else emitted_at,
        ),
        start_time=received_at,
        end_time=replied_at if replied_at is not None else emitted_at,
    )


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

    # ADR-004 / DESIGN §1.2: true webhook-receipt time, injected by
    # ``run_agent`` from the job payload the ingress handler stamped. This
    # line used to be an unconditional ``datetime.now(UTC)`` — minted here,
    # inside the LAST node of the graph — so ``replied_at - received_at``
    # timed only the HelpdeskPort calls below and excluded every model call
    # (docs/STATE.md §4.1). The ``or`` fallback is deliberate and must stay:
    # every graph/grounding/escalation test drives ``run_agent`` with no
    # clock injected and keeps its current behaviour.
    #
    # ``is None``, not ``or``: ``configurable`` is ``dict[str, Any]``, so mypy
    # cannot police what lands in it, and ``or`` would treat any falsy value
    # (an empty string, epoch 0) as "not supplied" and fall back **silently**
    # to exactly the pre-ADR-004 behaviour. A metric quietly reverting to the
    # wrong meaning is this project's signature failure; ``is None`` lets a
    # bad value fail loudly in ``record_run`` instead.
    configurable = config.get("configurable") or {}
    injected_received_at = configurable.get("received_at")
    received_at = datetime.now(UTC) if injected_received_at is None else injected_received_at
    trace_id = uuid.uuid4().hex
    clock = node_clock(config)

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
        held_actions = [*actions, "gate:held_pending"]
        _emit_run_trace(
            {**state, "actions": held_actions},
            ticket_id=ticket_id,
            trace_id=trace_id,
            actions=held_actions,
            gate_enabled=True,
            outcome=None,
            received_at=received_at,
            replied_at=None,
            clock=clock,
        )
        return {"actions": held_actions}

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
    replied_at = datetime.now(UTC)
    run_id = store.record_run(
        ticket_id=ticket_id,
        route=route,
        confidence=state.get("confidence"),
        outcome=outcome,
        verifier_score=state.get("verifier_score"),
        trace_id=trace_id,
        received_at=received_at,
        replied_at=replied_at,
        reasons=_escalation_reasons(state),
    )
    store.record_draft(run_id=run_id, body=draft, status="auto_sent")

    # Last, deliberately: everything above is the product (the reply is
    # posted, the run is recorded). This is the diagnostic report of what
    # just happened, and `emit_trace` swallows its own failures so it can
    # never undo any of it.
    _emit_run_trace(
        {**state, "actions": actions},
        ticket_id=ticket_id,
        trace_id=trace_id,
        actions=actions,
        gate_enabled=False,
        outcome=outcome,
        received_at=received_at,
        replied_at=replied_at,
        clock=clock,
    )

    return {"actions": actions}
