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

from typing import cast

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent import nodes
from agent.escalation_seam import EscalationDecider, PlaceholderEscalationDecider
from agent.llm import LLMClient
from agent.nodes import AgentDeps
from agent.state import RunState
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


def build_graph() -> CompiledStateGraph:
    """Compile the pinned pipeline once. Run-scoped collaborators (port,
    llm, escalation_decider, ticket_id) are supplied per-invocation via
    ``run_agent``'s ``config``, not baked in here — the compiled graph
    itself has no state of its own."""
    graph = StateGraph(RunState)

    graph.add_node("ingest", nodes.ingest)
    graph.add_node("classify", nodes.classify)
    graph.add_node("route", nodes.route_dispatch)
    graph.add_node("case_status", nodes.case_status)
    graph.add_node("permission", nodes.permission)
    graph.add_node("kb_answer", nodes.kb_answer)
    graph.add_node("off_topic", nodes.off_topic)
    graph.add_node("compose", nodes.compose)
    graph.add_node("verify", nodes.verify)
    graph.add_node("decide", nodes.decide)
    graph.add_node("act", nodes.act)

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "classify")
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
) -> RunState:
    """Public entrypoint: run one full agent turn for ``ticket_id`` and
    return the final ``RunState``. ``escalation_decider`` defaults to
    ``PlaceholderEscalationDecider`` — T-6 passes its real engine instead;
    no other argument or call site changes."""
    deps = AgentDeps(
        port=port,
        llm=llm,
        escalation_decider=escalation_decider or PlaceholderEscalationDecider(),
    )
    compiled = build_graph()
    result = compiled.invoke(
        {},
        config={"configurable": {"ticket_id": ticket_id, "deps": deps}},
    )
    return cast(RunState, result)
