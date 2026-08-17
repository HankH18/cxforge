"""Builds the LangGraph run — DESIGN §Agent graph, pinned verbatim:

    ingest -> classify -> route -> {case_status | permission | kb_answer |
    off_topic} -> compose -> verify -> decide -> act

Deliberately shallow (T-5 non-goal): no checkpointing, no interrupts, no
subgraphs. ``ticket_id`` and the run's collaborators (``HelpdeskPort``,
``LLMClient``, ``EscalationDecider``) are injected via LangGraph's
``RunnableConfig`` rather than folded into ``RunState`` — DESIGN pins
``RunState``'s field names exactly, and none of those four values is a
"grounding fact/output" ``RunState`` needs to carry; they're run-scoped
dependencies, which is exactly what ``config["configurable"]`` is for.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent import nodes
from agent.escalation_seam import EscalationDecider
from agent.llm import LLMClient
from agent.nodes import AgentDeps, NodeClock
from agent.state import RunState
from escalation.engine import EscalationEngine
from helpdesk.port import HelpdeskPort

_BRANCH_NODES = ("case_status", "permission", "kb_answer", "off_topic")

# Route value -> branch node name. Values are DESIGN's pinned `Route`
# literal; node names are the pinned pipeline's `{case_status | permission
# | kb_answer | off_topic}` set — "kb" (the value) intentionally differs
# from "kb_answer" (the node name), exactly as DESIGN spells the pipeline.
_ROUTE_TO_BRANCH = {
    "case_status": "case_status",
    "permission": "permission",
    "kb": "kb_answer",
    "off_topic": "off_topic",
}


def _select_branch(state: RunState) -> str:
    route = state["route"]
    branch = _ROUTE_TO_BRANCH.get(route or "")
    if branch is None:  # pragma: no cover - classify never emits an out-of-set route
        raise ValueError(f"classify produced an unroutable value: {route!r}")
    return branch


def _timed[NodeFn: Callable[..., dict[str, Any]]](name: str, node: NodeFn) -> NodeFn:
    """Wrap a node so the run's ``NodeClock`` learns when it really ran.

    Generic over the node's own callable type rather than a fixed
    ``Callable[[RunState, RunnableConfig], ...]`` alias: LangGraph matches
    ``add_node``'s overloads structurally, and a node passed in as a widened
    ``Callable`` stops satisfying them (mypy resolves the graph's state
    parameter to ``Never``). Handing back the same type it was given keeps
    every ``add_node`` call below type-checking exactly as the bare node did.

    This is the only place per-node wall time is measured, and it is
    measured by *being* the call: the interval starts immediately before the
    node body and is recorded in a ``finally``, so a timing exists because
    the node executed and cannot be produced any other way. That is the
    whole point — the Langfuse trace used to report 0–1ms per span for
    multi-second runs because the span objects were built after the fact in
    `act`, so their "duration" was the time it took to construct them
    (`agent.nodes`' trace section, ADR-006).

    ``finally`` rather than a plain sequence: a node that raises still
    records the interval it burned, which is what a hung or failing run's
    trace most needs to show. And with no clock in the config (a caller that
    compiled the graph itself) this is a pass-through — the wrapper never
    changes what a node returns or how it fails.
    """

    @functools.wraps(node)
    def timed_node(state: RunState, config: RunnableConfig) -> dict[str, Any]:
        clock = nodes.node_clock(config)
        if clock is None:
            return node(state, config)
        clock.enter(name)
        try:
            return node(state, config)
        finally:
            clock.leave(name)

    return cast(NodeFn, timed_node)


def build_graph() -> CompiledStateGraph:
    """Compile the pinned pipeline once. Run-scoped collaborators (port,
    llm, escalation_decider, ticket_id, node_clock) are supplied
    per-invocation via ``run_agent``'s ``config``, not baked in here — the
    compiled graph itself has no state of its own, which is also why the
    timing wrapper below holds no timings itself and only writes to the
    clock the running invocation brought with it."""
    graph = StateGraph(RunState)

    graph.add_node("ingest", _timed("ingest", nodes.ingest))
    graph.add_node("classify", _timed("classify", nodes.classify))
    graph.add_node("route", nodes.route_dispatch)
    graph.add_node("case_status", _timed("case_status", nodes.case_status))
    graph.add_node("permission", _timed("permission", nodes.permission))
    graph.add_node("kb_answer", _timed("kb_answer", nodes.kb_answer))
    graph.add_node("off_topic", _timed("off_topic", nodes.off_topic))
    graph.add_node("compose", _timed("compose", nodes.compose))
    graph.add_node("verify", _timed("verify", nodes.verify))
    graph.add_node("decide", _timed("decide", nodes.decide))
    graph.add_node("act", _timed("act", nodes.act))

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "classify")
    # `route` is deliberately untimed: it is a no-op dispatch node with no
    # observation of its own in `nodes._TRACE_NODE_KINDS`, so a timing for it
    # would have nowhere to go.
    graph.add_edge("classify", "route")
    graph.add_conditional_edges(
        "route",
        _select_branch,
        {branch: branch for branch in _BRANCH_NODES},
    )
    for branch in _BRANCH_NODES:
        graph.add_edge(branch, "compose")
    graph.add_edge("compose", "verify")
    graph.add_edge("verify", "decide")
    graph.add_edge("decide", "act")
    graph.add_edge("act", END)

    return graph.compile()


def run_agent(
    ticket_id: str,
    *,
    port: HelpdeskPort,
    llm: LLMClient,
    escalation_decider: EscalationDecider | None = None,
    received_at: datetime | None = None,
) -> RunState:
    """Public entrypoint: run one full agent turn for ``ticket_id`` and
    return the final ``RunState``. ``escalation_decider`` defaults to
    ``escalation.engine.EscalationEngine`` (T-6's real engine), built from
    the same ``llm`` passed here — so every graph/grounding test that
    passes a fake ``LLMClient`` for ``llm`` gets an engine backed by that
    same fake, never a second, independent OpenAI client. Pass
    ``escalation_decider`` explicitly (e.g.
    ``agent.escalation_seam.PlaceholderEscalationDecider()``) to bypass the
    real engine entirely.

    ``received_at`` (ADR-004, DESIGN §1.2) is the moment the Zendesk webhook
    was received, stamped in the ingress handler and carried here on the job
    payload. It is injected through ``config["configurable"]`` for the same
    reason ``ticket_id`` and ``deps`` are: it is a run-scoped dependency, not
    a grounding fact ``RunState`` should carry. ``act`` writes it to
    ``runs.received_at``, so ``replied_at - received_at`` finally spans the
    whole run — ingest, classify, retrieval, compose, verify, decide and act
    — instead of only the tail-end HelpdeskPort calls.

    ``None`` (the default) leaves ``act`` falling back to
    ``datetime.now(UTC)``. That fallback is load-bearing: every existing
    graph/grounding/escalation test calls ``run_agent`` without a clock and
    must keep passing unchanged.

    A fresh ``NodeClock`` is built here per invocation and injected the same
    way, so the per-node wall times the Langfuse trace reports are measured
    by the run that is happening rather than reconstructed afterwards (see
    ``_timed`` and ``agent.nodes.NodeClock``). Per invocation and not per
    compiled graph: a graph is compiled statelessly and could be reused, and
    a clock shared between two runs would report one run's timings on the
    other's trace."""
    deps = AgentDeps(
        port=port,
        llm=llm,
        escalation_decider=escalation_decider or EscalationEngine(llm=llm),
    )
    compiled = build_graph()
    result = compiled.invoke(
        {},
        config={
            "configurable": {
                "ticket_id": ticket_id,
                "deps": deps,
                "received_at": received_at,
                "node_clock": NodeClock(),
            }
        },
    )
    return cast(RunState, result)
