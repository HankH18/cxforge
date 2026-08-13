"""Pinned configuration constants for the agent graph.

Single source of truth for the three values DESIGN and the T-5 ticket
require to live in "one config constant" rather than scattered through the
graph:

- ``OPENAI_MODEL`` — DESIGN §LLMClient: "model version pinned in one config
  constant." Not itself pinned by DESIGN to a specific value; chosen here
  (see the module docstring in ``agent.llm``) and never exercised live in
  this environment (no ``OPENAI_API_KEY``).
- ``VERIFIER_THRESHOLD`` — DESIGN §Agent graph: "verifier_score < 0.7" is a
  hard escalation trigger for KB drafts.
- ``GATE_SETTING_KEY`` — the ``settings.key`` row the ``decide`` node reads
  R11's send/hold gate from. T-8 owns *writing* it (``PUT
  /api/settings/gate``); T-5 only reads it, defaulting OFF when absent.
"""

from __future__ import annotations

# DESIGN pins strict structured outputs + "one config constant" for the
# model version, not a specific model name. gpt-4o-mini-2024-07-18 is a
# deliberate choice (not DESIGN-pinned): SPEC's cost constraints apply
# broadly ("Actions cost is a real constraint") and a support-triage/
# templating workload doesn't need a larger model. Change only here.
OPENAI_MODEL = "gpt-4o-mini-2024-07-18"

# DESIGN §Agent graph, pinned verbatim: "threshold read from a config
# constant ... verifier_score < 0.7 is a hard escalation trigger."
VERIFIER_THRESHOLD = 0.7

# `settings.key` the `decide` node reads the boolean gate from (DESIGN
# §Portal API: `GET|PUT /api/settings/gate`). Chosen here since DESIGN
# doesn't pin the literal key string; documented so T-8 uses the same one.
GATE_SETTING_KEY = "gate_enabled"

# Placeholder escalation-group identity used by `act` when assigning a
# ticket via HelpdeskPort.assign_group. SPEC R6 names exactly one
# escalation group but DESIGN does not pin its provider group_id/name —
# that's an operational/deployment detail (a real Zendesk group id) nobody
# has assigned yet. Not exercised live in this environment.
DEFAULT_ESCALATION_GROUP_ID = "specialist-escalation"
DEFAULT_ESCALATION_GROUP_NAME = "Specialist Escalation"

# Number of KB chunks `search_kb` retrieves for grounding a kb-route answer
# or a permission always-grant check. Not DESIGN-pinned; matches
# `search_kb`'s own default of 5.
RETRIEVAL_K = 5
