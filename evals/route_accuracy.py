"""W1-E3 — route-classification accuracy against the LIVE model.

WHY THIS EXISTS
---------------
``docs/STATE.md §6.1``: all 78 graph/grounding/escalation tests drive a
``FakeLLMClient``, and every canonical-scenario test **hands the route in** via
a canned ``agent.schemas.Classification``. So route-classification accuracy —
the thing R2/R3/R4/R5 all hinge on — is measured by nothing. This module
measures it, by calling the real ``agent.nodes.classify`` node against a real
``agent.llm.AnthropicLLMClient``.

``docs/OWNER-ACTIONS.md`` OA-5: run this before booking camera time. A scenario
that the real model routes differently than the fakes assume is a filming
failure that no amount of green unit tests will surface.

Run it::

    set -a; source .env; set +a
    uv run python -m evals.route_accuracy                 # full 51-ticket sweep
    uv run python -m evals.route_accuracy --limit 5       # cheap smoke run

NO PARALLEL IMPLEMENTATION
--------------------------
This file contains **no classification logic of its own**. It builds a
``RunState``, hands it to ``agent.nodes.classify`` — the shipped node, with the
shipped ``agent.prompts.CLASSIFY_SYSTEM`` and the shipped
``agent.schemas.Classification`` schema — and records what comes back. If the
prompt changes, this measurement changes with it. That is the point: the same
discipline ``backend/tests/evals/test_no_divergence.py`` enforces on
``evals/report.py``.

``classify`` reaches its collaborators through ``config["configurable"]["deps"]``
(an ``agent.nodes.AgentDeps``). It only ever touches ``deps.llm``, so the
``port`` and ``escalation_decider`` slots are filled with ``_Unused`` sentinels
that raise on **any** attribute access — if a future ``classify`` starts using
them, this harness fails loudly instead of silently measuring something else.

WHAT IS SCORED, AND WHAT IS NOT (read before trusting a number)
---------------------------------------------------------------
``evals/labeled_set.yaml``'s ``expected_route`` is the **final** route of a run.
``classify`` structurally *cannot* emit ``"escalate"`` — its output schema is
typed against the narrower ``agent.state.ClassifyRoute`` (four branch values),
because DESIGN makes the escalation judgment T-6's, not the classifier's. So
the 51 labels split into two populations, reported separately and never mixed:

1. **SCORED — the headline number.** The tickets whose ``expected_route`` is one
   of the four branch routes. ``predicted == expected`` is a straight hit/miss,
   and the confusion matrix is 4x4. This is "route-classification accuracy".

2. **DIAGNOSTIC — the ``expected_route: escalate`` tickets.** Scoring these
   against ``"escalate"`` would mark all of them wrong by construction and make
   the headline meaningless. They are reported as a distribution instead, plus
   one genuinely load-bearing check: some escalations are *route-dependent*,
   because the condition that fires them is detected inside a specific branch
   node before ``EscalationEngine`` is ever consulted —

   | expected reason                          | detected by             | required route |
   |------------------------------------------|-------------------------|----------------|
   | ``unknown_case``                         | ``nodes.case_status``   | ``case_status``|
   | ``out_of_procedure``                     | ``nodes.permission``    | ``permission`` |
   | ``low_confidence`` + ``empty_retrieval`` | ``nodes.kb_answer``     | ``kb``         |
   | ``low_confidence`` + ``verifier_failure``| ``nodes.verify``        | ``kb``         |

   That mapping is not invented here: it is the same node->reason mapping
   ``evals/report.py::_structurally_unmeasured_reason`` already states, and the
   same id-naming convention ``backend/tests/evals/test_labeled_set.py``
   already relies on. If ``classify`` sends an ``esc-unknown_case-*`` ticket to
   ``kb``, the ``unknown_case`` trigger never fires and the ticket does not
   escalate — a real, silent R6 failure.

   Every other escalation reason (``billing``, ``human_request``,
   ``frustration``, ``complexity``, classifier abstention) is **route
   independent**: ``agent.nodes.decide`` calls
   ``EscalationDecider.evaluate`` unconditionally for every run that reaches it
   un-escalated, on all four branch routes alike (see that function's
   docstring). Those rows are counted but not pass/failed on route.

COST CONTROL — re-runnable without silently paying twice
---------------------------------------------------------
Every ``.structured()`` call goes through ``DiskCachedLLMClient``, keyed on
``(model, schema, canonical json of the exact messages the node built)`` and
**checkpointed to disk after every live call**. Consequences worth knowing:

* A re-run costs nothing for tickets already measured. An interrupted sweep
  resumes instead of restarting.
* Editing ``CLASSIFY_SYSTEM`` changes the messages, therefore the key, therefore
  the run re-measures — a stale cache can never launder a prompt change.
* ``--refresh`` forces a fresh sweep; ``--no-cache`` runs without persisting.
* ``live_call_count`` and the token/cost figures in ``results.json`` are the
  **actual** live calls this invocation made, read back from the Anthropic
  responses' own ``usage`` (see ``_instrument_usage``) — not an estimate.

TEST-ONLY escape hatch
----------------------
``EVALS_ROUTE_ACCURACY_FAKE_LLM_FOR_TESTS_ONLY`` substitutes a canned
``Classification`` so the offline suite can drive this module end to end with no
network and no key. Same two-signal guard as ``evals/report.py``'s: the variable
alone is not enough, the process must also be a real pytest process
(``PYTEST_VERSION``, set by pytest for the whole process lifetime). A stray
export in a shell running a real measurement is a loud error, not a fabricated
number.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel

# Same bootstrap, and the same reason, as evals/report.py: `uv run python -m
# evals.route_accuracy` puts the repo root on sys.path but not backend/src.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_SRC = _REPO_ROOT / "backend" / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# `agent` (the package) must finish initializing before `escalation.engine` is
# touched, or agent.graph <-> escalation.engine deadlock into a circular
# ImportError. `evals.report` imports escalation.engine, so `agent` goes first.
# Same pre-existing repo quirk documented in
# backend/tests/evals/test_no_divergence.py.
import agent  # noqa: E402, F401  (import-order load bearing — see above)
from agent import nodes  # noqa: E402
from agent.config import ANTHROPIC_MODEL  # noqa: E402
from agent.llm import AnthropicLLMClient, LLMClient  # noqa: E402
from agent.schemas import Classification  # noqa: E402
from agent.state import RunState  # noqa: E402
from evals.report import (  # noqa: E402
    build_ticket_and_conversation,
    load_labeled_set,
    writes_into_docs,
)

# The four values `classify` can actually emit — agent.state.ClassifyRoute.
BRANCH_ROUTES: tuple[str, ...] = ("case_status", "permission", "kb", "off_topic")

DEFAULT_OUTPUT_DIR = _REPO_ROOT / "evals" / "route-accuracy"
DEFAULT_CACHE_NAME = "cache.json"
LABELED_SET_RELPATH = Path("evals/labeled_set.yaml")

# claude-opus-5 list price, USD per million tokens, verified 2026-08-16.
# Used only to turn the measured token counts into a dollar figure; the token
# counts themselves are read back from the API responses.
PRICE_USD_PER_MTOK: dict[str, tuple[float, float]] = {"claude-opus-5": (5.00, 25.00)}

TEST_ONLY_FAKE_LLM_ENV_VAR = "EVALS_ROUTE_ACCURACY_FAKE_LLM_FOR_TESTS_ONLY"

# The id markers evals/labeled_set.yaml uses to distinguish low_confidence's
# three subtypes — the same convention backend/tests/evals/test_labeled_set.py
# and evals/report.py already rely on. "abstention" is deliberately absent: it
# is produced by EscalationEngine itself and is route independent.
_KB_NODE_LOW_CONFIDENCE_MARKERS = ("empty_retrieval", "verifier_failure")


def _running_under_pytest() -> bool:
    """Second, independent signal that this really is a test process.

    Identical mechanism and identical reasoning to
    ``evals.report._running_under_pytest``: ``PYTEST_VERSION`` is set by pytest
    for the whole process lifetime, so the fake-classification hatch cannot be
    satisfied by a stray export in a shell running a real measurement.
    """
    return "PYTEST_VERSION" in os.environ


# ---------------------------------------------------------------------------
# LLM plumbing — a disk cache and a usage recorder around the shipped client
# ---------------------------------------------------------------------------


@dataclass
class UsageTotals:
    """Token counters read back from the Anthropic responses themselves."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    api_calls: int = 0

    def cost_usd(self, model: str) -> float | None:
        price = PRICE_USD_PER_MTOK.get(model)
        if price is None:
            return None
        in_price, out_price = price
        billable_in = self.input_tokens + self.cache_creation_input_tokens
        return round(
            (billable_in / 1_000_000) * in_price + (self.output_tokens / 1_000_000) * out_price,
            6,
        )


def _instrument_usage(client: AnthropicLLMClient, totals: UsageTotals) -> None:
    """Record real token usage by wrapping the SDK's ``messages.parse``.

    ``LLMClient.structured`` is pinned by DESIGN to return only the parsed
    model, so usage is not reachable through the seam — and widening that
    signature to get a cost number would be a contract change this track does
    not own (``docs/BUILD-PLAN.md §1``). Wrapping the underlying SDK method
    here keeps the instrumentation entirely inside ``evals/`` and leaves
    ``backend/src/agent/llm.py`` untouched. Reaching through ``_get_client`` is
    deliberate and local to this harness.
    """
    raw = client._get_client()
    original_parse = raw.messages.parse

    def recording_parse(**kwargs: Any) -> Any:
        response = original_parse(**kwargs)
        totals.api_calls += 1
        usage = getattr(response, "usage", None)
        if usage is not None:
            totals.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            totals.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
            totals.cache_read_input_tokens += int(
                getattr(usage, "cache_read_input_tokens", 0) or 0
            )
            totals.cache_creation_input_tokens += int(
                getattr(usage, "cache_creation_input_tokens", 0) or 0
            )
        return response

    raw.messages.parse = recording_parse  # type: ignore[method-assign]


class DiskCachedLLMClient:
    """Persistent memoizing ``agent.llm.LLMClient`` decorator.

    Keyed on ``(provenance, model, schema qualname, canonical json of
    messages)`` and flushed to disk after **every** live call, so an interrupted
    sweep resumes and a re-run of an unchanged prompt costs nothing.
    ``live_call_count`` is the exact number of network calls this invocation
    made.

    ``provenance`` is ``"live"`` for the real client and ``"fake"`` for the
    TEST-ONLY canned client, and it is part of the key AND stored in the file.
    Without it a test run could write fabricated verdicts into the same cache a
    real measurement later reads — reintroducing, through the back door, exactly
    the fabricated-numbers defect that T-21 removed from ``evals/report.py``. A
    cache file whose provenance does not match this run is ignored, not merged.
    """

    def __init__(
        self,
        inner: LLMClient,
        *,
        model: str,
        cache_path: Path | None,
        refresh: bool = False,
        provenance: str = "live",
    ) -> None:
        self._inner = inner
        self._model = model
        self._cache_path = cache_path
        self._provenance = provenance
        self._cache: dict[str, dict[str, Any]] = {}
        self.live_call_count = 0
        self.cache_hit_count = 0
        if cache_path is not None and cache_path.exists() and not refresh:
            try:
                raw = json.loads(cache_path.read_text())
                entries = raw.get("entries")
                if isinstance(entries, dict) and raw.get("provenance") == provenance:
                    self._cache = entries
            except (json.JSONDecodeError, OSError):
                # A corrupt cache is a cost problem, never a correctness one —
                # start clean rather than refuse to measure.
                self._cache = {}

    def _key(self, schema: type[BaseModel], messages: list[dict[str, Any]]) -> str:
        return json.dumps(
            {
                "provenance": self._provenance,
                "model": self._model,
                "schema": schema.__qualname__,
                "messages": messages,
            },
            sort_keys=True,
            default=str,
        )

    def _flush(self) -> None:
        if self._cache_path is None:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "provenance": self._provenance,
                    "model": self._model,
                    "entries": self._cache,
                },
                indent=2,
                sort_keys=True,
            )
        )
        tmp.replace(self._cache_path)

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        key = self._key(schema, messages)
        cached = self._cache.get(key)
        if cached is not None:
            self.cache_hit_count += 1
            return schema(**cached)
        result = self._inner.structured(schema, messages, temperature)
        self._cache[key] = result.model_dump()
        self.live_call_count += 1
        self._flush()
        return result


@dataclass
class _FakeClassifyLLMClient:
    """TEST-ONLY double — see the module docstring's escape-hatch section.

    ``matches`` is an ordered list of ``(substring, classification payload)``;
    the first substring found anywhere in the assembled user message wins, else
    ``default``. Never used by a real ``uv run python -m evals.route_accuracy``.
    """

    default: dict[str, Any]
    matches: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        if schema is not Classification:
            raise AssertionError(
                f"_FakeClassifyLLMClient only supports Classification, got {schema.__name__}"
            )
        blob = "\n".join(str(m.get("content", "")) for m in messages)
        for needle, payload in self.matches:
            if needle in blob:
                return Classification(**payload)
        return Classification(**self.default)


class _Unused:
    """Sentinel for an ``AgentDeps`` slot ``classify`` must never touch.

    ``classify`` reads ``deps.llm`` and — since W2-B4 — exactly one method on
    ``deps.port``. Filling every other slot with something that raises on any
    attribute access makes that a structural fact rather than an assumption:
    if a future ``classify`` starts using one, this harness stops with a clear
    error instead of quietly measuring a different thing. It did exactly that
    when W2-B4 landed, which is why ``_NoHistoryPort`` exists below rather
    than a broader stub having been slipped in.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        raise AssertionError(
            f"evals/route_accuracy.py drives agent.nodes.classify only, which must not touch "
            f"deps.{self._name} (attempted: .{attr}). If classify now needs it, this harness "
            f"needs a real collaborator — do not stub one in silently."
        )


class _NoHistoryPort(_Unused):
    """The narrowest port ``classify`` can be driven with (W2-B4/ADR-009).

    Answers ``fetch_requester_history`` with an empty list — the true answer
    for this harness, since ``evals/labeled_set.yaml`` is a flat list of
    independent tickets with no requester threading — and inherits
    ``_Unused``'s raise-on-anything-else behaviour for every other method.
    """

    def __init__(self) -> None:
        super().__init__("port")

    def fetch_requester_history(
        self, requester_email: str, *, exclude_ticket_id: str, limit: int = 5
    ) -> list[Any]:
        return []


# ---------------------------------------------------------------------------
# Driving the shipped classify node
# ---------------------------------------------------------------------------


@dataclass
class RouteResult:
    ticket_id: str
    expected_route: str
    expected_reasons: list[str]
    predicted_route: str | None
    confidence: float | None
    topic: str | None
    case_id: str | None
    scored: bool
    correct: bool | None
    required_branch_route: str | None
    branch_route_ok: bool | None
    error: str | None = None


def required_branch_route(ticket: dict[str, Any]) -> str | None:
    """The branch node ``classify`` MUST pick for this escalation to fire.

    ``None`` means the escalation is route independent — ``agent.nodes.decide``
    runs ``EscalationDecider.evaluate`` unconditionally on every un-escalated
    route, so billing / human_request / frustration / complexity / classifier
    abstention escalate regardless of which branch was chosen.

    The mapping below is read off the shipped code, not invented: see
    ``evals/report.py::_structurally_unmeasured_reason`` for the same
    node->reason correspondence and ``agent.nodes.decide``'s docstring for the
    unconditional-evaluate rule.
    """
    reasons = ticket.get("expected_reasons") or []
    ticket_id = ticket["id"]
    if "unknown_case" in reasons:
        return "case_status"
    if "out_of_procedure" in reasons:
        return "permission"
    if "low_confidence" in reasons and any(
        marker in ticket_id for marker in _KB_NODE_LOW_CONFIDENCE_MARKERS
    ):
        return "kb"
    return None


def classify_ticket(ticket: dict[str, Any], *, llm: LLMClient) -> dict[str, Any]:
    """Call the SHIPPED ``agent.nodes.classify`` on one labeled-set row."""
    ticket_obj, conversation = build_ticket_and_conversation(ticket)
    state: RunState = {
        "ticket": ticket_obj,
        "conversation": conversation,
        "tool_results": {},
        "actions": [],
    }
    config: Any = {
        "configurable": {
            "ticket_id": ticket["id"],
            "deps": nodes.AgentDeps(
                # W2-B4 / ADR-009 gave `classify` a second collaborator: it
                # now asks the port for the requester's prior tickets and
                # puts them in the classifier's context. So `port` can no
                # longer be an `_Unused` sentinel.
                #
                # `_NoHistoryPort` answers "no prior contact", which is the
                # TRUE answer for this harness rather than a convenient one:
                # evals/labeled_set.yaml is a flat list of independent
                # tickets with no requester threading at all, and
                # `build_ticket_and_conversation` synthesizes one ticket per
                # row. Every other port method stays an `_Unused` sentinel,
                # so if `classify` ever starts calling one of those, this
                # harness still fails loudly instead of measuring something
                # subtly different from the shipped node.
                port=_NoHistoryPort(),  # type: ignore[arg-type]
                llm=llm,
                escalation_decider=_Unused("escalation_decider"),  # type: ignore[arg-type]
            ),
        }
    }
    return nodes.classify(state, config)


def measure(tickets: list[dict[str, Any]], *, llm: LLMClient) -> list[RouteResult]:
    results: list[RouteResult] = []
    for ticket in tickets:
        expected = ticket["expected_route"]
        scored = expected in BRANCH_ROUTES
        needed = None if scored else required_branch_route(ticket)
        try:
            update = classify_ticket(ticket, llm=llm)
        except Exception as exc:  # noqa: BLE001 — a failed row must not lose the sweep
            results.append(
                RouteResult(
                    ticket_id=ticket["id"],
                    expected_route=expected,
                    expected_reasons=list(ticket.get("expected_reasons") or []),
                    predicted_route=None,
                    confidence=None,
                    topic=None,
                    case_id=None,
                    scored=scored,
                    correct=False if scored else None,
                    required_branch_route=needed,
                    branch_route_ok=False if needed else None,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        predicted = update["route"]
        results.append(
            RouteResult(
                ticket_id=ticket["id"],
                expected_route=expected,
                expected_reasons=list(ticket.get("expected_reasons") or []),
                predicted_route=predicted,
                confidence=update.get("confidence"),
                topic=update.get("topic"),
                case_id=(update.get("tool_results") or {}).get("case_id_hint"),
                scored=scored,
                correct=(predicted == expected) if scored else None,
                required_branch_route=needed,
                branch_route_ok=(predicted == needed) if needed else None,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def confusion_matrix(results: list[RouteResult]) -> dict[str, dict[str, int]]:
    """expected -> predicted -> count, over the SCORED rows only."""
    matrix = {exp: dict.fromkeys([*BRANCH_ROUTES, "error"], 0) for exp in BRANCH_ROUTES}
    for row in results:
        if not row.scored:
            continue
        predicted = row.predicted_route or "error"
        matrix[row.expected_route][predicted] = matrix[row.expected_route].get(predicted, 0) + 1
    return matrix


def per_route_scores(matrix: dict[str, dict[str, int]]) -> dict[str, dict[str, float | int]]:
    scores: dict[str, dict[str, float | int]] = {}
    for route in BRANCH_ROUTES:
        tp = matrix[route].get(route, 0)
        support = sum(matrix[route].values())
        predicted_as = sum(matrix[exp].get(route, 0) for exp in BRANCH_ROUTES)
        recall = tp / support if support else 0.0
        precision = tp / predicted_as if predicted_as else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        scores[route] = {
            "support": support,
            "predicted_as": predicted_as,
            "true_positives": tp,
            "recall": round(recall, 4),
            "precision": round(precision, 4),
            "f1": round(f1, 4),
        }
    return scores


def escalate_diagnostics(results: list[RouteResult]) -> dict[str, Any]:
    escalate_rows = [r for r in results if not r.scored]
    distribution = Counter(r.predicted_route or "error" for r in escalate_rows)
    route_dependent = [r for r in escalate_rows if r.required_branch_route is not None]
    hits = [r for r in route_dependent if r.branch_route_ok]
    return {
        "total": len(escalate_rows),
        "classify_route_distribution": dict(sorted(distribution.items())),
        "route_dependent_total": len(route_dependent),
        "route_dependent_correct": len(hits),
        "route_dependent_accuracy": (
            round(len(hits) / len(route_dependent), 4) if route_dependent else None
        ),
        "route_dependent_misses": [
            {
                "id": r.ticket_id,
                "required_route": r.required_branch_route,
                "predicted_route": r.predicted_route,
                "expected_reasons": r.expected_reasons,
            }
            for r in route_dependent
            if not r.branch_route_ok
        ],
    }


def summarize(results: list[RouteResult]) -> dict[str, Any]:
    scored = [r for r in results if r.scored]
    correct = [r for r in scored if r.correct]
    matrix = confusion_matrix(results)
    return {
        "scored_sample_size": len(scored),
        "scored_correct": len(correct),
        "route_accuracy": round(len(correct) / len(scored), 4) if scored else None,
        "confusion_matrix": matrix,
        "per_route": per_route_scores(matrix),
        "escalate_diagnostics": escalate_diagnostics(results),
        "errors": [
            {"id": r.ticket_id, "error": r.error} for r in results if r.error is not None
        ],
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    matrix = summary["confusion_matrix"]
    esc = summary["escalate_diagnostics"]
    accuracy = summary["route_accuracy"]
    lines: list[str] = [
        "# Route-classification accuracy (W1-E3)",
        "",
        f"Generated {payload['generated_at']} — model `{payload['model']}`.",
        "",
        "Measured by calling the shipped `agent.nodes.classify` node against a live",
        "`agent.llm.AnthropicLLMClient` over `evals/labeled_set.yaml`. No fakes, no",
        "handed-in `Classification`. See `evals/route_accuracy.py`'s module docstring",
        "for what is scored and what is diagnostic.",
        "",
        "## Headline",
        "",
        f"- **Route accuracy: {accuracy if accuracy is not None else 'n/a'}** "
        f"({summary['scored_correct']}/{summary['scored_sample_size']} branch-route tickets)",
        f"- Labeled-set rows considered: {payload['tickets_considered']}",
        f"- Live model calls this run: {payload['live_call_count']} "
        f"(cache hits: {payload['cache_hit_count']})",
        f"- Measured cost this run: {payload['cost']['usd']} USD "
        f"({payload['cost']['input_tokens']} in / {payload['cost']['output_tokens']} out tokens)",
        "",
    ]
    if payload["live_call_count"] == 0 and payload["cache_hit_count"]:
        lines += [
            "> Every verdict above was replayed from "
            f"`{payload['cache_path']}`, so this invocation spent nothing. The numbers are "
            "still live-model measurements — the cache stores what the model actually "
            "returned, keyed on the exact prompt, so a prompt edit invalidates it and "
            "forces a real re-measurement. Use `--refresh` to re-measure anyway.",
            "",
        ]
    lines += [
        "## Confusion matrix — expected (rows) x predicted (columns)",
        "",
        "| expected \\ predicted | " + " | ".join(BRANCH_ROUTES) + " | error | recall |",
        "|---|" + "---|" * (len(BRANCH_ROUTES) + 2),
    ]
    for expected in BRANCH_ROUTES:
        row = matrix[expected]
        cells = [str(row.get(p, 0)) for p in BRANCH_ROUTES]
        recall = summary["per_route"][expected]["recall"]
        lines.append(
            f"| **{expected}** | " + " | ".join(cells) + f" | {row.get('error', 0)} | {recall} |"
        )
    lines += [
        "",
        "## Per-route precision / recall / F1",
        "",
        "| route | support | precision | recall | F1 |",
        "|---|---|---|---|---|",
    ]
    for route in BRANCH_ROUTES:
        s = summary["per_route"][route]
        lines.append(
            f"| {route} | {s['support']} | {s['precision']} | {s['recall']} | {s['f1']} |"
        )
    lines += [
        "",
        "## Diagnostic — `expected_route: escalate` tickets",
        "",
        "`classify` cannot emit `escalate` (its schema is `agent.state.ClassifyRoute`), so",
        "these are NOT scored against the headline. What matters is whether the branch it",
        "picks can still detect the escalation condition.",
        "",
        f"- Escalate-labeled tickets: {esc['total']}",
        f"- Route-dependent subset: {esc['route_dependent_total']} "
        f"(accuracy {esc['route_dependent_accuracy']})",
        "",
        "Route distribution `classify` chose for escalate-labeled tickets:",
        "",
        "| classify route | count |",
        "|---|---|",
    ]
    for route, count in esc["classify_route_distribution"].items():
        lines.append(f"| {route} | {count} |")
    if esc["route_dependent_misses"]:
        lines += [
            "",
            "### Route-dependent misses — these tickets would NOT escalate",
            "",
            "| ticket | required route | classify chose | expected reasons |",
            "|---|---|---|---|",
        ]
        for miss in esc["route_dependent_misses"]:
            lines.append(
                f"| `{miss['id']}` | {miss['required_route']} | {miss['predicted_route']} | "
                f"{', '.join(miss['expected_reasons'])} |"
            )
    else:
        lines += ["", "No route-dependent misses."]

    misses = [r for r in payload["results"] if r["scored"] and not r["correct"]]
    if misses:
        lines += [
            "",
            "## Every scored miss",
            "",
            "| ticket | expected | predicted | confidence |",
            "|---|---|---|---|",
        ]
        for miss in misses:
            lines.append(
                f"| `{miss['ticket_id']}` | {miss['expected_route']} | "
                f"{miss['predicted_route']} | {miss['confidence']} |"
            )
    if summary["errors"]:
        lines += ["", "## Errors", ""]
        for err in summary["errors"]:
            lines.append(f"- `{err['id']}`: {err['error']}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_llm(model: str, cache_path: Path | None, refresh: bool) -> tuple[Any, UsageTotals]:
    totals = UsageTotals()
    fake_json = os.environ.get(TEST_ONLY_FAKE_LLM_ENV_VAR)
    if fake_json and not _running_under_pytest():
        raise RuntimeError(
            f"{TEST_ONLY_FAKE_LLM_ENV_VAR} is set, but this is not a pytest process. That "
            "variable substitutes a canned Classification for every ticket; honouring it "
            "here would produce a route-accuracy number that measured nothing. Unset it to "
            "run a real measurement."
        )
    if fake_json:
        payload = json.loads(fake_json)
        fake = _FakeClassifyLLMClient(
            default=payload["default"],
            matches=[(str(n), p) for n, p in payload.get("matches", [])],
        )
        # The cache path is honoured for the fake too, so the checkpoint/resume
        # behaviour is exercised offline — safe because `provenance="fake"` is
        # part of every key and of the file, so a fake entry can never be read
        # back by a real measurement.
        return (
            DiskCachedLLMClient(
                fake, model=model, cache_path=cache_path, refresh=refresh, provenance="fake"
            ),
            totals,
        )

    # override=False: a key already in the environment always beats .env.
    load_dotenv(dotenv_path=_REPO_ROOT / ".env", override=False)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set (checked the process environment and "
            f"{_REPO_ROOT / '.env'}). E3 measures the REAL model — a route harness driven "
            "by a fake client would measure nothing. Prefix with "
            "`set -a; source .env; set +a` and re-run."
        )
    inner = AnthropicLLMClient(model=model)
    _instrument_usage(inner, totals)
    return DiskCachedLLMClient(inner, model=model, cache_path=cache_path, refresh=refresh), totals


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.route_accuracy",
        description="Route-classification accuracy of the live model over evals/labeled_set.yaml",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "measure only the first N labeled-set rows, in file order. For smoke runs; a "
            "headline number must come from a full sweep, because the file is grouped by "
            "category and a prefix is not a representative sample."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"where results.json / report.md are written (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument("--cache-path", type=Path, default=None, help="override the cache file")
    parser.add_argument(
        "--no-cache", action="store_true", help="do not read or write the on-disk cache"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="ignore cached results and re-measure live"
    )
    parser.add_argument("--model", default=ANTHROPIC_MODEL, help=f"default: {ANTHROPIC_MODEL}")
    parser.add_argument(
        "--min-accuracy",
        type=float,
        default=None,
        help="exit non-zero if route accuracy falls below this (default: never gate)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    output_dir: Path = args.output_dir
    if writes_into_docs(output_dir):
        raise SystemExit(
            f"refusing to write under docs/ (got {output_dir}). docs/eval-report/ is an "
            "approved, published artifact and regenerating it is W3-G1, not this harness. "
            "Point --output-dir somewhere else."
        )

    cache_path: Path | None
    if args.no_cache:
        cache_path = None
    else:
        cache_path = args.cache_path or (output_dir / DEFAULT_CACHE_NAME)

    _, tickets = load_labeled_set()
    if args.limit is not None:
        tickets = tickets[: args.limit]

    llm, totals = _resolve_llm(args.model, cache_path, args.refresh)
    results = measure(tickets, llm=llm)
    summary = summarize(results)

    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": args.model,
        "labeled_set": str(LABELED_SET_RELPATH),
        "tickets_considered": len(tickets),
        "limit": args.limit,
        "live_call_count": llm.live_call_count,
        "cache_hit_count": llm.cache_hit_count,
        "cache_path": str(cache_path) if cache_path else None,
        "cost": {
            "usd": totals.cost_usd(args.model),
            "input_tokens": totals.input_tokens,
            "output_tokens": totals.output_tokens,
            "cache_read_input_tokens": totals.cache_read_input_tokens,
            "cache_creation_input_tokens": totals.cache_creation_input_tokens,
            "api_calls": totals.api_calls,
            "price_basis_usd_per_mtok": PRICE_USD_PER_MTOK.get(args.model),
        },
        "summary": summary,
        "results": [asdict(r) for r in results],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    (output_dir / "report.md").write_text(render_report(payload))

    accuracy = summary["route_accuracy"]
    print(f"route accuracy: {accuracy} over {summary['scored_sample_size']} scored tickets")
    print(f"live calls: {llm.live_call_count} (cache hits {llm.cache_hit_count})")
    print(f"cost: {payload['cost']['usd']} USD")
    print(f"wrote {output_dir / 'results.json'} and {output_dir / 'report.md'}")

    if args.min_accuracy is not None and (accuracy is None or accuracy < args.min_accuracy):
        print(f"FAIL: route accuracy {accuracy} below --min-accuracy {args.min_accuracy}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
