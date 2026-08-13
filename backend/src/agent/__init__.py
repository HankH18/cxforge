"""Agent core: the LangGraph run and the LLMClient isolation layer.

T-5 (DESIGN §Agent graph, §LLMClient). The pinned pipeline is
``ingest -> classify -> route -> {case_status | permission | kb_answer |
off_topic} -> compose -> verify -> decide -> act`` — see ``agent.graph``
for how it's wired, ``agent.nodes`` for each step, ``agent.state`` for the
pinned ``RunState``/``Route``, ``agent.templates`` for where R9's
template-fill invariant is enforced, and ``agent.escalation_seam`` for the
T-6 insertion point.
"""

from __future__ import annotations

from agent.escalation_seam import (
    EscalationDecider,
    EscalationDecision,
    EscalationTrigger,
    PlaceholderEscalationDecider,
)
from agent.graph import build_graph, run_agent
from agent.llm import LLMClient, OpenAILLMClient
from agent.state import Route, RunState

__all__ = [
    "EscalationDecider",
    "EscalationDecision",
    "EscalationTrigger",
    "LLMClient",
    "OpenAILLMClient",
    "PlaceholderEscalationDecider",
    "Route",
    "RunState",
    "build_graph",
    "run_agent",
]
