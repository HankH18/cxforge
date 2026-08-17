"""Pinned configuration constants for the agent graph.

Single source of truth for the three values DESIGN and the T-5 ticket
require to live in "one config constant" rather than scattered through the
graph:

- ``ANTHROPIC_MODEL`` / ``ANTHROPIC_MAX_TOKENS`` — DESIGN §LLMClient:
  "model version pinned in one config constant." DESIGN pins the
  *requirement*, not a provider or a specific model name, so the authorised
  pivot from OpenAI to Anthropic changes the value here and nothing else.
  See the module docstring in ``agent.llm``.
- ``VERIFIER_THRESHOLD`` — DESIGN §Agent graph: "verifier_score < 0.7" is a
  hard escalation trigger for KB drafts.
- ``GATE_SETTING_KEY`` — the ``settings.key`` row the ``decide`` node reads
  R11's send/hold gate from. T-8 owns *writing* it (``PUT
  /api/settings/gate``); T-5 only reads it, defaulting OFF when absent.
"""

from __future__ import annotations

from data.embeddings import default_min_score

# DESIGN pins strict structured outputs + "one config constant" for the
# model version, not a provider or a specific model name. Change only here.
ANTHROPIC_MODEL = "claude-opus-5"

# Anthropic counts thinking tokens against max_tokens, and thinking is on by
# default on this model — so this is sized for "reasoning plus a structured
# verdict", not for the verdict alone. Too small a budget truncates the
# response mid-structure, which surfaces as a refusal-shaped ValueError from
# AnthropicLLMClient rather than as an obvious error.
ANTHROPIC_MAX_TOKENS = 16000

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

# How many of the requester's prior tickets `classify` puts in front of the
# classifier (ADR-009 / BUILD-PLAN §1.5, whose signature defaults `limit` to
# the same 5). Small on purpose: this is "has this person been here before,
# and about what", not a case file. Every extra row is prompt cost on every
# single run, and a long history would start to compete with the message
# actually being classified.
REQUESTER_HISTORY_LIMIT = 5

# BUILD-PLAN §1.3 / ADR-010's relevance floor: chunks scoring below this are
# dropped, so `search_kb` can return nothing at all and R6's `empty_retrieval`
# hard trigger becomes reachable (`docs/STATE.md §6.4` records that it was
# previously unreachable).
#
# Unlike every other constant in this module this is NOT a literal pinned
# here, and deliberately so. A cosine cutoff is only meaningful relative to
# the embedding space that produced the score: the lexical HashingEmbedder's
# correct hits land at 0.17-0.38 while VoyageEmbedder's land at 0.29-0.63,
# so one hard-coded number would either be a no-op for one embedder or
# reject every result from the other. The calibrated value therefore lives
# beside the embedder it was measured on (see each class's `min_score` in
# `data.embeddings`, which records the measurement), and this name resolves
# whichever one the configured embedder carries. `data` cannot import from
# `agent`, so the dependency necessarily points this way.
#
# Resolved once, at import. `search_kb` does not read this name — it reads
# the floor off the embedder instance it actually used, so the two can never
# disagree even if `KB_EMBEDDER` changes mid-process (which only a test
# does). This constant is the config surface BUILD-PLAN §1.3 names.
KB_MIN_SCORE: float = default_min_score()
