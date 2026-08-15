"""T-7/T-21 escalation eval report generator — DESIGN §Verification strategy /
SPEC R15: confusion matrix, precision/recall/F1, and a PR curve image with
the chosen threshold marked, computed against ``evals/labeled_set.yaml``.

Run as::

    uv run python -m evals.report

from the repo root. Writes ``docs/eval-report/report.md``,
``docs/eval-report/pr_curve.png``, and ``docs/eval-report/metrics.json``.

T-21 REWRITE — this module used to grade a PARALLEL implementation of the
escalation precedence (three hand-authored replay tables:
``STUB_CLASSIFIER_VERDICTS``, ``STUB_STRUCTURAL_REASON``,
``STUB_ABSTENTION_IDS``), never once calling the shipped engine. Those
tables are gone, not bypassed. Every prediction below comes from calling
``escalation.engine.EscalationEngine.evaluate``/``.decide`` DIRECTLY — this
file contains no escalation decision logic of its own (see
``backend/tests/evals/test_no_divergence.py`` for the structural proof).

WHAT IS REAL VS UNMEASURED, PER TIER (read this before trusting a number)
--------------------------------------------------------------------------
DESIGN's seven hard triggers split into three groups, by how this report
drives ``EscalationEngine``:

1. **REAL, no LLM call** — billing / human_request. ``EscalationEngine.
   evaluate()`` checks these itself (``agent.escalation_seam.
   detect_all_deterministic_hard_rules``, pure regex over the ticket body)
   before it ever looks at a trigger or a classifier. Calling ``.evaluate()``
   is enough; this file adds nothing.

2. **REAL, no LLM call, one independent check** — unknown_case.
   ``EscalationEngine`` has no code of its own that resolves a case; in
   production that judgment is made upstream by ``agent.nodes.case_status``/
   ``agent.nodes._resolve_case`` and handed down as an
   ``agent.escalation_seam.EscalationTrigger``. This report reproduces the
   simplest slice of that judgment — a referenced ``MFG-####-####`` id
   checked for membership in ``fixtures/cases.yaml`` directly (see
   ``detect_unknown_case_trigger``) — a SIMPLIFIED version of
   ``_resolve_case`` (it does not also check a requester email against the
   case; ``evals/labeled_set.yaml`` rows carry no such field) but genuinely
   independent of this file's own labels. The resulting trigger is then
   handed to ``EscalationEngine.decide()`` — the real engine method for
   "a trigger already fired" — so the ESCALATE decision itself is still
   100% the engine's, never this file's.

3. **REAL, live Anthropic classifier** — frustration / complexity /
   classifier abstention. Any ticket that reaches tier 3 (no hard rule
   fired) is scored by calling ``EscalationEngine.evaluate()``, which calls
   ``escalation.classifier.run_classifier`` against a real
   ``agent.llm.AnthropicLLMClient`` (see "Live LLM usage" below for cost
   control). Whatever the model actually says — including a genuine
   abstention — is what gets reported. Nothing here is canned.

4. **UNMEASURED — out_of_procedure, and low_confidence's two structural
   subtypes (empty KB retrieval, verifier/groundedness failure)**.
   Unlike unknown_case, these three conditions are not simple membership
   checks: in production they are the OUTPUT of ``agent.nodes.permission``
   (a live, KB-grounded permission-match LLM call over retrieved policy
   chunks), ``agent.nodes.kb_answer`` (a live pgvector search), and
   ``agent.nodes.verify`` (compose a draft, then a live groundedness-judge
   LLM call over it) respectively — real judgment calls that live entirely
   in ``agent.nodes``, not in ``escalation.engine``. ``EscalationEngine.
   evaluate``/``.decide`` have no code path that re-derives any of this;
   reproducing it here would mean re-implementing three ``agent.nodes``
   pipelines inside an eval script, which is both out of T-21's scope
   (``evals/report.py`` + tests + docs only — not ``backend/src/agent/**``)
   and exactly the "parallel implementation" risk this ticket exists to
   remove. So this report does not attempt it: tickets whose
   ``expected_reasons`` name ``out_of_procedure``, or name
   ``low_confidence`` with ``empty_retrieval``/``verifier_failure`` in
   their id (the SAME id-naming convention
   ``backend/tests/evals/test_labeled_set.py`` already relies on to
   distinguish low_confidence's three subtypes), are marked
   ``measured=False`` and EXCLUDED from every metric below — confusion
   matrix, precision/recall/F1, and hard-trigger recall alike — rather than
   silently defaulted to a guessed answer. They are listed by id, with the
   reason, in both ``report.md`` and ``metrics.json``
   (``unmeasured_ticket_ids``). Partial honest coverage beats complete fake
   coverage — see docs/tickets.json T-21's own instructions.
   ``esc-low_confidence-abstention-garbled-01`` (the third low_confidence
   subtype) is NOT in this excluded set: classifier abstention is produced
   by ``EscalationEngine.evaluate()`` itself (tier 3 above), so it is
   genuinely measured, whatever the live model actually does with it.

Live LLM usage / cost control
------------------------------
Every ``.structured()`` call this report makes goes through
``CachingLLMClient``, a thin memoizing decorator keyed on
``(schema, messages)``. This matters because the threshold sweep
(``sweep_thresholds``) constructs up to 101 ``EscalationEngine`` instances
— one per threshold step — and calls ``.evaluate()`` on every measured
ticket at every step; without caching that would be up to 101x the live
calls for no reason, since a ticket's classifier verdict does not depend on
the threshold, only whether it CLEARS the threshold does (and
``EscalationEngine`` itself, not this file, does that comparison). With
caching, each ticket that reaches the classifier tier costs exactly ONE
live call for the whole run, however many threshold steps or however many
times its prediction is computed. Plus one live preflight call
(``_verify_live_key``) made once per run, before any ticket is scored, to
turn an unusable key into a loud failure instead of 40-some silent
"classifier abstained" results indistinguishable from a real one (see
below). ``metrics.json``'s ``live_classifier_call_count`` field reports the
exact per-ticket count the run actually made (excludes the preflight call).

``topic`` (threaded into the classifier's prompt) is each ticket's own
``subject`` line, not an LLM-derived summary — ``agent.nodes.classify``
produces a real ``topic`` via its own separate LLM call in production, but
adding that call here would be an unrelated, unnecessary cost for this
ticket's purpose (measuring the ESCALATION engine, not the classify node).
Documented here as a known simplification, not hidden. Likewise, the
synthetic ``helpdesk.models.Ticket``/``Message`` objects
(``build_ticket_and_conversation``) carry placeholder metadata (email,
timestamps) — ``EscalationEngine.evaluate``/``.decide`` never read
``ticket`` at all (see ``escalation/engine.py``'s own module docstring),
so this is inert, not a shortcut on anything the engine actually consults.

FAIL LOUDLY without a usable key
----------------------------------
``_resolve_llm_client`` requires ``ANTHROPIC_API_KEY`` (loaded from the
repo's gitignored ``.env`` via ``python-dotenv`` if not already in the
environment) and refuses to proceed at all without it — no fallback to
fabricated verdicts. It also makes one live preflight call and refuses if
THAT fails: without this check, a present-but-invalid key would raise
``anthropic.AuthenticationError`` on every real ticket call, which
``escalation.classifier.run_classifier`` (T-18's own narrowed except
clause) legitimately absorbs into ``None`` — i.e. classifier abstention, a
REAL, DESIGN-pinned hard-escalation outcome. That absorption is correct
behavior for the shipped classifier, but it would make a broken key
indistinguishable from ~40 genuine abstentions in this report's output.
The preflight call closes that gap: a key that cannot make one real call
fails the whole run loudly, before any ticket is ever scored.

TEST-ONLY escape hatch: if the environment variable named by
``TEST_ONLY_FAKE_LLM_ENV_VAR`` is set to a JSON object shaped like
``EscalationCall`` (``{"escalate": ..., "reasons": [...], "confidence":
...}``), that FIXED verdict is returned for every classifier call instead
of resolving a real key — no network, no ``ANTHROPIC_API_KEY`` needed. This
exists solely so ``backend/tests/evals/test_report.py`` can exercise this
script's plumbing (the approval gate, the docs-write guard, metrics.json's
shape, the PR curve image) via real subprocess invocations without making
live API calls on every test run (T-21: "the unit suite must not make live
API calls"). It is NOT a per-ticket answer key like the deleted stub
tables — it is one uniform, test-supplied verdict applied identically to
every ticket that reaches the classifier tier, and it never substitutes for
a real run: a bare ``uv run python -m evals.report`` invocation never sets
this variable, so the published ``docs/eval-report/`` artifacts always come
from the real classifier.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

# `uv run python -m evals.report` adds the repo root (cwd) to sys.path, but
# NOT backend/src — that only happens automatically under pytest, via
# [tool.pytest.ini_options].pythonpath in pyproject.toml. This script is run
# directly, not under pytest, so it sets up its own import path first. Every
# import that depends on this MUST come after it (hence the noqa: E402s
# below) — see scripts/live_smoke.py for the same repo's precedent of a
# deferred/guarded import for a script run outside pytest.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_SRC = _REPO_ROOT / "backend" / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from agent.escalation_seam import EscalationTrigger  # noqa: E402
from agent.llm import AnthropicLLMClient, LLMClient  # noqa: E402
from escalation.config import CLASSIFIER_CONFIDENCE_THRESHOLD  # noqa: E402
from escalation.engine import EscalationEngine  # noqa: E402
from escalation.schemas import EscalationCall  # noqa: E402
from helpdesk.models import Message, Ticket  # noqa: E402

LABELED_SET_PATH = _REPO_ROOT / "evals" / "labeled_set.yaml"
CASES_PATH = _REPO_ROOT / "fixtures" / "cases.yaml"
DOCS_DIR = _REPO_ROOT / "docs"
OUTPUT_DIR = DOCS_DIR / "eval-report"

CASE_ID_RE = re.compile(r"MFG-\d{4}-\d{4}")

HARD_TRIGGER_REASONS = {
    "billing",
    "human_request",
    "unknown_case",
    "out_of_procedure",
    "low_confidence",
}

DRAFT_WATERMARK = "DRAFT — LABELS NOT YET APPROVED"

# See module docstring's "TEST-ONLY escape hatch" section.
TEST_ONLY_FAKE_LLM_ENV_VAR = "EVALS_REPORT_FAKE_LLM_FOR_TESTS_ONLY"


def _running_under_pytest() -> bool:
    """True only while this process is inside a live pytest run.

    The fake-verdict escape hatch above is what lets the unit suite exercise
    the classifier tier without spending money on live calls. Gated on the
    env var ALONE it would be convention, not structure — and it would
    reintroduce, in a more dangerous form, the exact defect T-21 exists to
    remove: an exported or leaked EVALS_REPORT_FAKE_LLM_FOR_TESTS_ONLY would
    make a FINAL-bannered report out of entirely fabricated verdicts, with
    nothing in the artifacts saying so. Moving fabrication from a hardcoded
    table into an environment variable is not deleting it.

    So the hatch requires a second, independent signal that this really is a
    test process. Same mechanism and same reasoning as T-24's schema
    override in backend/src/data/db.py: PYTEST_VERSION is set by pytest
    itself for the WHOLE process lifetime (unlike PYTEST_CURRENT_TEST, which
    is per test item and absent during session hooks), so it cannot be
    satisfied by a stray export in a shell that is running the real report.
    """
    return "PYTEST_VERSION" in os.environ

# id-naming markers for low_confidence's two STRUCTURAL subtypes (see module
# docstring point 4) — the SAME convention
# backend/tests/evals/test_labeled_set.py's LOW_CONFIDENCE_SUBTYPE_ID_MARKERS
# already relies on. Deliberately excludes "abstention": that subtype IS
# measured (tier 3, via the live classifier call inside evaluate()).
_LOW_CONFIDENCE_UNMEASURED_ID_MARKERS = ("empty_retrieval", "verifier_failure")

_EVAL_REQUESTER_EMAIL = "eval-harness@othram.invalid"
_EVAL_PLACEHOLDER_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_labeled_set(path: Path = LABELED_SET_PATH) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = yaml.safe_load(path.read_text())
    header = {"approval": raw.get("approval", {}), "meta": raw.get("meta", {})}
    tickets: list[dict[str, Any]] = raw["tickets"]
    return header, tickets


def load_known_case_ids(path: Path = CASES_PATH) -> set[str]:
    raw = yaml.safe_load(path.read_text())
    return {c["case_id"] for c in raw["cases"]}


def is_approved(header: dict[str, Any]) -> bool:
    """The refuse-vs-final gate. True only if a human has EXPLICITLY flipped
    ``approval.status`` to ``APPROVED`` and recorded who and when — anything
    else (missing header, ``PROPOSED_AWAITING_HUMAN_REVIEW``, an
    ``APPROVED`` status with no name/date attached) is treated as NOT
    approved. This is the one function that decides whether the rest of
    this module renders a DRAFT-watermarked report or a final one — see
    ``report_status_banner``/``render_report``."""
    approval = header.get("approval") or {}
    return (
        approval.get("status") == "APPROVED"
        and bool(approval.get("approved_by"))
        and bool(approval.get("approved_date"))
    )


def canonical_approval_header() -> dict[str, Any]:
    """The approval gate's ONLY input (T-25 acceptance 1).

    ``--labeled-set`` substitutes the set that is *scored and rendered*; it
    must never be able to answer the question "has a human approved these
    labels?". That question is settled exclusively by the committed
    ``evals/labeled_set.yaml`` at ``LABELED_SET_PATH``, so pointing the CLI
    at a doctored copy with ``approval.status: APPROVED`` yields a DRAFT
    render and a non-zero exit like any other unapproved run. To exercise
    the approved direction of the gate, a test builds a synthetic repo tree
    whose own ``evals/labeled_set.yaml`` is the approved fixture — there is
    no flag, and no environment variable, that redirects this read."""
    header, _ = load_labeled_set(LABELED_SET_PATH)
    return header


def writes_into_docs(output_dir: Path) -> bool:
    """True when ``output_dir`` lands anywhere under the repo's ``docs/``.

    ``docs/`` is the published, human-facing tree; T-25 acceptance 2 keeps
    unapproved numbers out of it entirely rather than relying on a DRAFT
    watermark inside a file that lives among the real deliverables."""
    try:
        output_dir.resolve().relative_to(DOCS_DIR.resolve())
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# LLMClient resolution — real Anthropic client (cached, preflight-checked)
# or, TEST-ONLY, a fixed canned verdict. See module docstring.
# ---------------------------------------------------------------------------


class CachingLLMClient:
    """Memoizing ``agent.llm.LLMClient`` decorator, keyed on
    ``(schema.__qualname__, canonical json of messages)``. See module
    docstring's "Live LLM usage / cost control" section for why this
    exists: the threshold sweep calls ``EscalationEngine.evaluate()`` for
    every measured ticket at every threshold step, and a ticket's
    classifier verdict does not depend on the threshold — only whether the
    (real) engine decides it clears the threshold does. Wraps any
    ``LLMClient``; every call this report makes to the model goes through
    one instance of this, so ``.live_call_count`` is the exact number of
    real network calls the whole run made (excluding the one-off preflight
    check — see ``call_uncached``)."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self._cache: dict[tuple[str, str], BaseModel] = {}
        self.live_call_count = 0

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        key = (schema.__qualname__, json.dumps(messages, sort_keys=True, default=str))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        result = self._inner.structured(schema, messages, temperature)
        self._cache[key] = result
        self.live_call_count += 1
        return result

    def call_uncached(self, schema: type[BaseModel], messages: list[dict[str, Any]]) -> BaseModel:
        """Bypasses both the cache and ``.live_call_count`` — used only by
        ``_verify_live_key``'s one-off preflight probe, so it never pollutes
        the per-ticket call count this report surfaces in metrics.json."""
        return self._inner.structured(schema, messages)


@dataclass
class _FixedVerdictLLMClient:
    """TEST-ONLY double — see module docstring's "TEST-ONLY escape hatch".
    Returns the SAME canned ``EscalationCall`` for every call, regardless of
    message content. Never used by a real ``uv run python -m evals.report``
    invocation."""

    verdict: EscalationCall

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        if schema is not EscalationCall:
            raise AssertionError(
                f"_FixedVerdictLLMClient only supports EscalationCall, got {schema.__name__}"
            )
        return self.verdict


def _verify_live_key(llm: CachingLLMClient) -> None:
    """One real, uncached probe call. See module docstring's "FAIL LOUDLY
    without a usable key" section for why this exists: without it, an
    invalid key would make every classifier-tier ticket silently read as a
    genuine classifier abstention (a real, absorbable outcome
    ``escalation.classifier.run_classifier`` legitimately produces) rather
    than the setup failure it actually is."""
    probe_messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "Reply with escalate=false, reasons=[], confidence=1.0.",
        },
        {
            "role": "user",
            "content": "evals/report.py preflight key check — not a real support ticket.",
        },
    ]
    try:
        llm.call_uncached(EscalationCall, probe_messages)
    except Exception as exc:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is set but a live preflight call to the Anthropic API "
            f"failed ({type(exc).__name__}: {exc}). Refusing to proceed — see this "
            "module's docstring for why a broken key must fail loudly here rather than "
            "silently reading as classifier abstention on every ticket."
        ) from exc


def _resolve_llm_client() -> LLMClient:
    """Real ``AnthropicLLMClient`` (cached, preflight-verified), or —
    TEST-ONLY — a fixed canned verdict. See module docstring."""
    fake_verdict_json = os.environ.get(TEST_ONLY_FAKE_LLM_ENV_VAR)
    if fake_verdict_json and not _running_under_pytest():
        raise RuntimeError(
            f"{TEST_ONLY_FAKE_LLM_ENV_VAR} is set, but this is not a pytest process. "
            "That variable substitutes a canned classifier verdict for every ticket; "
            "honouring it here would produce a report whose numbers are fabricated — "
            "the exact defect T-21 removed when it deleted the stub verdict tables. "
            "Unset it to run a real measurement."
        )
    if fake_verdict_json:
        payload = json.loads(fake_verdict_json)
        return CachingLLMClient(_FixedVerdictLLMClient(EscalationCall(**payload)))

    # override=False: an ANTHROPIC_API_KEY already in the environment always
    # wins over .env — never silently replaced by a stale file value.
    load_dotenv(dotenv_path=_REPO_ROOT / ".env", override=False)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set (checked the process environment and "
            f"{_REPO_ROOT / '.env'}). T-21 deleted this report's hand-authored stub "
            "verdict tables — the classifier half now requires a real, working "
            "Anthropic key. Set ANTHROPIC_API_KEY and re-run; this report refuses to "
            "silently substitute fabricated verdicts for a missing credential."
        )
    caching = CachingLLMClient(AnthropicLLMClient())
    _verify_live_key(caching)
    return caching


# ---------------------------------------------------------------------------
# Prediction — calls escalation.engine.EscalationEngine directly. No
# escalation decision logic lives in this file; see
# backend/tests/evals/test_no_divergence.py.
# ---------------------------------------------------------------------------


@dataclass
class Prediction:
    measured: bool
    escalate: bool | None
    reasons: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)
    unmeasured_reason: str | None = None


def build_ticket_and_conversation(ticket: dict[str, Any]) -> tuple[Ticket, list[Message]]:
    """A synthetic ``Ticket``/``Message`` pair satisfying ``helpdesk.
    models``' pinned shape — see module docstring: ``EscalationEngine.
    evaluate``/``.decide`` never read ``ticket`` at all, and only ever read
    ``conversation``/``topic``, so the placeholder metadata here is inert."""
    ticket_obj = Ticket(
        id=ticket["id"],
        subject=ticket["subject"],
        requester_email=_EVAL_REQUESTER_EMAIL,
        status="open",
        tags=[],
        created_at=_EVAL_PLACEHOLDER_TIMESTAMP,
    )
    message = Message(
        id=f"{ticket['id']}-body",
        author_kind="customer",
        text=ticket["body"],
        public=True,
        created_at=_EVAL_PLACEHOLDER_TIMESTAMP,
    )
    return ticket_obj, [message]


def detect_unknown_case_trigger(
    ticket: dict[str, Any], known_case_ids: set[str]
) -> EscalationTrigger | None:
    """REAL, independent stand-in for ``agent.nodes._resolve_case``'s "case
    not found" outcome — see module docstring point 2. No LLM, no live DB:
    a referenced ``MFG-####-####`` id is checked for membership in
    ``fixtures/cases.yaml`` directly."""
    match = CASE_ID_RE.search(ticket["body"])
    if match is None:
        return None
    case_id = match.group(0)
    if case_id in known_case_ids:
        return None
    return EscalationTrigger(
        reason="unknown_case",
        detail=(
            f"[REAL, independent of the label] referenced case id {case_id!r} is absent "
            "from fixtures/cases.yaml"
        ),
    )


def _structurally_unmeasured_reason(ticket: dict[str, Any]) -> str | None:
    """See module docstring point 4. Returns a human-readable reason string
    if ``ticket`` represents a scenario this report cannot drive
    ``EscalationEngine`` to reproduce (out_of_procedure, or low_confidence's
    empty-retrieval/verifier-failure subtypes), else ``None``."""
    reasons = ticket.get("expected_reasons", [])
    ticket_id = ticket["id"]
    if "out_of_procedure" in reasons:
        return (
            "out_of_procedure is detected by agent.nodes.permission (a live, "
            "KB-grounded permission-match LLM call over retrieved policy chunks) "
            "BEFORE EscalationEngine is ever consulted — EscalationEngine has no code "
            "path that re-derives this judgment."
        )
    if "low_confidence" in reasons and any(
        marker in ticket_id for marker in _LOW_CONFIDENCE_UNMEASURED_ID_MARKERS
    ):
        return (
            "this low_confidence scenario is detected by agent.nodes.kb_answer (empty "
            "pgvector KB retrieval) or agent.nodes.verify (a live groundedness-judge LLM "
            "call over a composed draft) BEFORE EscalationEngine is ever consulted — same "
            "structural gap as out_of_procedure."
        )
    return None


def predict_ticket(
    ticket: dict[str, Any], *, engine: EscalationEngine, known_case_ids: set[str]
) -> Prediction:
    unmeasured_reason = _structurally_unmeasured_reason(ticket)
    if unmeasured_reason is not None:
        return Prediction(measured=False, escalate=None, unmeasured_reason=unmeasured_reason)

    ticket_obj, conversation = build_ticket_and_conversation(ticket)
    topic = ticket["subject"]  # see module docstring's "Live LLM usage" section
    trigger = detect_unknown_case_trigger(ticket, known_case_ids)

    if trigger is not None:
        decision = engine.decide(
            trigger=trigger,
            ticket=ticket_obj,
            conversation=conversation,
            topic=topic,
            tool_results={},
        )
    else:
        decision = engine.evaluate(
            ticket=ticket_obj, conversation=conversation, topic=topic, tool_results={}
        )

    return Prediction(
        measured=True,
        escalate=decision.escalate,
        reasons=[t.reason for t in decision.triggers],
        detail=[t.detail for t in decision.triggers],
    )


def run_predictions(
    tickets: list[dict[str, Any]], *, engine: EscalationEngine, known_case_ids: set[str]
) -> dict[str, Prediction]:
    return {
        t["id"]: predict_ticket(t, engine=engine, known_case_ids=known_case_ids) for t in tickets
    }


# ---------------------------------------------------------------------------
# Metrics — every function below takes an already-MEASURED ticket list
# (unmeasured tickets excluded by the caller; see main()).
# ---------------------------------------------------------------------------


def confusion_matrix(
    tickets: list[dict[str, Any]], predictions: dict[str, Prediction]
) -> dict[str, int]:
    tp = fp = fn = tn = 0
    for t in tickets:
        pred = predictions[t["id"]]
        assert pred.measured, f"{t['id']} is unmeasured — exclude before confusion_matrix()"
        expected = bool(t["expected_escalate"])
        predicted = bool(pred.escalate)
        if expected and predicted:
            tp += 1
        elif not expected and predicted:
            fp += 1
        elif expected and not predicted:
            fn += 1
        else:
            tn += 1
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def precision_recall_f1(cm: dict[str, int]) -> tuple[float, float, float]:
    tp, fp, fn = cm["tp"], cm["fp"], cm["fn"]
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def hard_trigger_recall(
    tickets: list[dict[str, Any]], predictions: dict[str, Prediction]
) -> tuple[float | None, int]:
    """Recall restricted to MEASURED tickets whose label is a genuine
    DESIGN hard trigger (billing/human_request/unknown_case/
    out_of_procedure/low_confidence) — excludes frustration/complexity-only
    escalations, which are classifier judgment, not a hard trigger. Callers
    always pass the already-measured ticket list (see main()), so
    out_of_procedure and low_confidence's two structural subtypes are
    already absent from ``tickets`` here — this is what T-21's module
    docstring calls "measured-only": the number is real (against the real
    engine, per T-7 acceptance 4 / T-21 acceptance 5), but it is silent
    about three scenarios this report cannot drive the engine to
    reproduce. See ``main()``'s ``unmeasured_tickets`` and report.md's own
    "Unmeasured scenarios" section for that count."""
    subset = [
        t
        for t in tickets
        if t["expected_escalate"] and set(t["expected_reasons"]) & HARD_TRIGGER_REASONS
    ]
    if not subset:
        return None, 0
    hits = sum(1 for t in subset if predictions[t["id"]].escalate)
    return hits / len(subset), len(subset)


def label_distribution(tickets: list[dict[str, Any]]) -> dict[str, Any]:
    routes = Counter(t["expected_route"] for t in tickets)
    reasons = Counter(r for t in tickets for r in t["expected_reasons"])
    escalate = sum(1 for t in tickets if t["expected_escalate"])
    return {
        "total": len(tickets),
        "routes": dict(sorted(routes.items())),
        "escalate": escalate,
        "not_escalate": len(tickets) - escalate,
        "reasons": dict(sorted(reasons.items())),
    }


def sweep_thresholds(
    tickets: list[dict[str, Any]], known_case_ids: set[str], llm: LLMClient, *, steps: int = 101
) -> list[dict[str, Any]]:
    """Sweeps ``CLASSIFIER_CONFIDENCE_THRESHOLD`` by constructing a fresh
    ``EscalationEngine`` per threshold value and calling it directly — see
    module docstring's "Live LLM usage / cost control": ``llm`` is expected
    to be a ``CachingLLMClient``, so the (up to 101x) repeated calls this
    makes per ticket cost exactly one real network call each, not 101."""
    results = []
    for threshold in np.linspace(0.0, 1.0, steps):
        threshold = float(threshold)
        engine = EscalationEngine(llm=llm, threshold=threshold)
        predictions = run_predictions(tickets, engine=engine, known_case_ids=known_case_ids)
        measured = [t for t in tickets if predictions[t["id"]].measured]
        cm = confusion_matrix(measured, predictions)
        precision, recall, f1 = precision_recall_f1(cm)
        results.append(
            {"threshold": threshold, "precision": precision, "recall": recall, "f1": f1, "cm": cm}
        )
    return results


def recommend_threshold(sweep: list[dict[str, Any]]) -> float:
    """The threshold maximizing F1 across the sweep; ties broken toward the
    SMALLEST threshold (prefer catching more classifier-judged cases when
    several thresholds tie on F1). This is a RECOMMENDATION only —
    ``CLASSIFIER_CONFIDENCE_THRESHOLD`` in backend/src/escalation/config.py
    stays untouched (out of this ticket's scope); see the report's own
    "Recommended threshold" section."""
    best = max(sweep, key=lambda r: (r["f1"], -r["threshold"]))
    return best["threshold"]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def report_status_banner(approved: bool) -> str:
    if approved:
        return "FINAL — labels approved (see evals/labeled_set.yaml's approval: block)"
    return f"{DRAFT_WATERMARK} (see evals/labeled_set.yaml's approval: block and evals/REVIEW.md)"


def plot_pr_curve(
    sweep: list[dict[str, Any]], recommended_threshold: float, approved: bool, out_path: Path
) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    recalls = [r["recall"] for r in sweep]
    precisions = [r["precision"] for r in sweep]
    recommended = min(sweep, key=lambda r: abs(r["threshold"] - recommended_threshold))

    fig, ax = plt.subplots(figsize=(7, 6))
    ax.plot(
        recalls,
        precisions,
        "-",
        color="#2b6cb0",
        linewidth=2,
        label="Precision/Recall (threshold sweep)",
    )
    ax.scatter(
        [recommended["recall"]],
        [recommended["precision"]],
        color="crimson",
        zorder=5,
        s=70,
        label=(
            f"recommended threshold={recommended['threshold']:.2f} "
            f"(P={recommended['precision']:.2f}, R={recommended['recall']:.2f})"
        ),
    )
    ax.set_xlabel("Recall")
    ax.set_ylabel("Precision")
    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.02, 1.02)
    title = "Escalation decision — Precision/Recall curve\n"
    title += "measured against the real engine — 3 structural scenarios unmeasured, see report"
    ax.set_title(title, fontsize=10)
    ax.legend(loc="lower left", fontsize=8)
    ax.grid(alpha=0.3)

    if not approved:
        fig.text(
            0.5,
            0.5,
            "DRAFT\nLABELS NOT YET APPROVED",
            fontsize=30,
            color="red",
            alpha=0.35,
            ha="center",
            va="center",
            rotation=30,
            weight="bold",
        )

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def render_report(
    *,
    header: dict[str, Any],
    tickets: list[dict[str, Any]],
    predictions: dict[str, Prediction],
    cm: dict[str, int],
    precision: float,
    recall: float,
    f1: float,
    hard_recall: float | None,
    hard_n: int,
    recommended_threshold: float,
    distribution: dict[str, Any],
    approved: bool,
    image_filename: str,
    measured_tickets: list[dict[str, Any]],
    unmeasured_tickets: list[dict[str, Any]],
    run_started_at: datetime,
    live_classifier_call_count: int,
) -> str:
    # The DRAFT paragraph must quote the status of the file the gate actually
    # consulted, which is always the canonical set — never ``header``, which
    # is whatever --labeled-set pointed at (T-25 acceptance 1).
    approval = canonical_approval_header().get("approval", {})
    banner = report_status_banner(approved)
    lines: list[str] = []
    lines.append("# Escalation eval report — T-7 / T-21")
    lines.append("")
    lines.append(f"**{banner}**")
    lines.append("")
    if not approved:
        lines.append(
            "> This report is generated end-to-end from `evals/labeled_set.yaml`, but that "
            "file's `approval.status` is "
            f"`{approval.get('status')!r}`, not `APPROVED`. Every number below is a DRAFT — "
            "proof the pipeline runs, not a real measurement. See `evals/REVIEW.md` for what "
            "the project owner needs to review before this can become a final report."
        )
        lines.append("")
    scored_approval = (header.get("approval") or {}).get("status")
    if scored_approval != approval.get("status"):
        lines.append(
            "> NOTE: the scored labeled set was substituted via `--labeled-set` and carries "
            f"`approval.status` `{scored_approval!r}`. The substitution changes only which "
            "tickets were scored below; it has no bearing on the approval banner above, which "
            "reads `evals/labeled_set.yaml` and nothing else."
        )
        lines.append("")
    lines.append(f"Generated: {run_started_at.isoformat()}")
    lines.append(f"Labeled tickets: {distribution['total']}")
    lines.append("")

    lines.append("## Methodology — what is REAL vs UNMEASURED")
    lines.append("")
    lines.append(
        "Every prediction below comes from calling `escalation.engine.EscalationEngine."
        "evaluate`/`.decide` directly — this report has no escalation decision logic of its "
        "own (see `backend/tests/evals/test_no_divergence.py`)."
    )
    lines.append("")
    lines.append(
        "- **REAL, no LLM call** — `billing` / `human_request`: checked by "
        "`EscalationEngine.evaluate()` itself, pure regex over the ticket body."
    )
    lines.append(
        "- **REAL, no LLM call** — `unknown_case`: a referenced `MFG-####-####` case id is "
        "checked for membership in `fixtures/cases.yaml` directly (independent of the label), "
        "then handed to `EscalationEngine.decide()` as a real trigger."
    )
    lines.append(
        "- **REAL, live Anthropic classifier** — `frustration` / `complexity` / classifier "
        "abstention: any ticket that reaches this tier is scored by a real "
        "`agent.llm.AnthropicLLMClient` call inside `EscalationEngine.evaluate()`. Whatever "
        "the model says — including a genuine abstention — is what gets reported."
    )
    lines.append(
        "- **UNMEASURED** — `out_of_procedure`, and `low_confidence`'s empty-retrieval / "
        "verifier-failure subtypes: these are detected by `agent.nodes` (a live pgvector KB "
        "search, plus a permission-match or groundedness-judge LLM call), not by "
        "`EscalationEngine` — there is no code path in the engine that reproduces this "
        "judgment, and reproducing `agent.nodes`' own pipelines here is out of this report's "
        "scope. See `evals/report.py`'s module docstring for the full rationale."
    )
    lines.append("")
    lines.append(
        f"**Live classifier calls this run made: {live_classifier_call_count}** "
        "(cached per ticket across the full threshold sweep — see module docstring)."
    )
    lines.append("")

    lines.append("## Label distribution")
    lines.append("")
    lines.append(f"- Total tickets: {distribution['total']}")
    lines.append(
        f"- Escalate: {distribution['escalate']} / Not escalate: {distribution['not_escalate']}"
    )
    lines.append("- By route:")
    for route, count in distribution["routes"].items():
        lines.append(f"  - `{route}`: {count}")
    lines.append("- By reason (a ticket can carry more than one):")
    for reason, count in distribution["reasons"].items():
        lines.append(f"  - `{reason}`: {count}")
    lines.append("")

    lines.append("## Unmeasured scenarios (excluded from every metric below)")
    lines.append("")
    if not unmeasured_tickets:
        lines.append("None — every ticket in this labeled set was measured.")
    else:
        lines.append(
            f"{len(unmeasured_tickets)} of {distribution['total']} tickets require "
            "`agent.nodes`-level infrastructure this report does not drive (see Methodology "
            "above) and are excluded from the confusion matrix, precision/recall/F1, and "
            "hard-trigger recall below — not defaulted to a guessed answer."
        )
        lines.append("")
        for t in unmeasured_tickets:
            lines.append(f"- `{t['id']}` — {predictions[t['id']].unmeasured_reason}")
    lines.append("")

    lines.append(
        f"## Confusion matrix (binary escalate / not-escalate, {len(measured_tickets)} "
        "measured tickets, at recommended threshold)"
    )
    lines.append("")
    lines.append("| | Predicted escalate | Predicted no-escalate |")
    lines.append("|---|---|---|")
    lines.append(f"| **Actual escalate** | TP={cm['tp']} | FN={cm['fn']} |")
    lines.append(f"| **Actual no-escalate** | FP={cm['fp']} | TN={cm['tn']} |")
    lines.append("")

    lines.append("## Precision / Recall / F1 (at recommended threshold, measured tickets only)")
    lines.append("")
    lines.append(f"- Precision: {precision:.3f}")
    lines.append(f"- Recall: {recall:.3f}")
    lines.append(f"- F1: {f1:.3f}")
    lines.append("")

    lines.append("## Hard-trigger subset recall (measured tickets only)")
    lines.append("")
    if hard_recall is None:
        lines.append("No measured hard-trigger tickets in this labeled set.")
    else:
        lines.append(
            f"Recall on the {hard_n} MEASURED tickets labeled with a genuine DESIGN hard "
            "trigger (billing/human_request/unknown_case/out_of_procedure/low_confidence, "
            f"excluding frustration/complexity-only escalations): **{hard_recall:.3f}**, "
            f"measured against the real engine (T-7 acceptance 4 / T-21 acceptance 5)."
        )
        lines.append(
            "This is the real number, whatever it is — T-21's own non-goal is explicit that "
            "a miss here is a finding to report, not a threshold/subset to adjust. It does "
            "NOT include the out_of_procedure / low_confidence-structural tickets listed "
            "above as unmeasured; see that section for why, and treat this recall figure as "
            "coverage of a real subset, not the full DESIGN hard-trigger set."
        )
    lines.append("")

    lines.append("## Recommended threshold")
    lines.append("")
    lines.append(
        f"Sweeping `CLASSIFIER_CONFIDENCE_THRESHOLD` over the real classifier's scores "
        f"(measured tickets only) and maximizing F1 recommends **{recommended_threshold:.2f}** "
        f"(current provisional value in `backend/src/escalation/config.py`: "
        f"{CLASSIFIER_CONFIDENCE_THRESHOLD:.2f})."
    )
    lines.append(
        "**This value is NOT written to `backend/src/escalation/config.py`.** T-21's scope is "
        "`evals/report.py` + tests + `docs/eval-report/` only — this report computes and "
        "states a recommendation, machine-checkable in `metrics.json`, never commits it."
    )
    lines.append("")

    lines.append("## PR curve")
    lines.append("")
    lines.append(f"![PR curve]({image_filename})")
    lines.append("")

    lines.append("## Per-ticket predictions")
    lines.append("")
    lines.append("| id | expected | measured | predicted | engine triggers |")
    lines.append("|---|---|---|---|---|")
    for t in tickets:
        p = predictions[t["id"]]
        exp = "escalate" if t["expected_escalate"] else "no-escalate"
        if not p.measured:
            lines.append(f"| `{t['id']}` | {exp} | no | UNMEASURED | — |")
            continue
        pred = "escalate" if p.escalate else "no-escalate"
        mark = "" if exp == pred else " **MISMATCH**"
        triggers = "; ".join(p.detail) or "—"
        lines.append(f"| `{t['id']}` | {exp} | yes | {pred}{mark} | {triggers} |")
    lines.append("")

    lines.append("---")
    lines.append(f"**{banner}**")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labeled-set", type=Path, default=LABELED_SET_PATH)
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    # The gate reads the committed set; args.labeled_set only drives what is
    # scored and rendered (T-25 acceptance 1 — see canonical_approval_header).
    canonical_header = canonical_approval_header()
    approved = is_approved(canonical_header)

    # Nothing unapproved reaches docs/ — not even watermarked (T-25
    # acceptance 2). A draft render is still available, but the caller has to
    # name an --output-dir outside docs/ and take the non-zero exit with it.
    if not approved and writes_into_docs(args.output_dir):
        print(
            f"REFUSING to write to {args.output_dir}: {LABELED_SET_PATH} is not "
            "human-approved, and docs/ holds published deliverables only. Re-run with "
            "--output-dir pointing outside docs/ for a DRAFT render (still exits non-zero), "
            "or get the labels approved first (see evals/REVIEW.md).",
            file=sys.stderr,
        )
        return 1

    # A live (or TEST-ONLY fixed) classifier is required for EVERY real
    # generation, approved or not — see module docstring's "FAIL LOUDLY"
    # section. Nothing is written if this fails.
    try:
        llm = _resolve_llm_client()
    except RuntimeError as exc:
        print(f"REFUSING to run the escalation eval report: {exc}", file=sys.stderr)
        return 1

    header, tickets = load_labeled_set(args.labeled_set)
    known_case_ids = load_known_case_ids(args.cases)
    run_started_at = datetime.now(UTC)

    sweep = sweep_thresholds(tickets, known_case_ids, llm)
    recommended = recommend_threshold(sweep)
    engine = EscalationEngine(llm=llm, threshold=recommended)
    predictions = run_predictions(tickets, engine=engine, known_case_ids=known_case_ids)
    measured_tickets = [t for t in tickets if predictions[t["id"]].measured]
    unmeasured_tickets = [t for t in tickets if not predictions[t["id"]].measured]

    cm = confusion_matrix(measured_tickets, predictions)
    precision, recall, f1 = precision_recall_f1(cm)
    hard_recall, hard_n = hard_trigger_recall(measured_tickets, predictions)
    distribution = label_distribution(tickets)

    live_call_count = llm.live_call_count if isinstance(llm, CachingLLMClient) else 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    image_path = plot_pr_curve(sweep, recommended, approved, args.output_dir / "pr_curve.png")

    report_md = render_report(
        header=header,
        tickets=tickets,
        predictions=predictions,
        cm=cm,
        precision=precision,
        recall=recall,
        f1=f1,
        hard_recall=hard_recall,
        hard_n=hard_n,
        recommended_threshold=recommended,
        distribution=distribution,
        approved=approved,
        image_filename=image_path.name,
        measured_tickets=measured_tickets,
        unmeasured_tickets=unmeasured_tickets,
        run_started_at=run_started_at,
        live_classifier_call_count=live_call_count,
    )
    (args.output_dir / "report.md").write_text(report_md)

    metrics = {
        "approved": approved,
        # Sourced from the canonical set, not args.labeled_set: a doctored
        # copy must not be able to stamp "APPROVED" into the artifacts either.
        "approval_status": canonical_header.get("approval", {}).get("status"),
        "scored_labeled_set": str(args.labeled_set),
        "scored_labeled_set_is_canonical": args.labeled_set.resolve() == LABELED_SET_PATH.resolve(),
        "confusion_matrix": cm,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "hard_trigger_recall": hard_recall,
        "hard_trigger_subset_size": hard_n,
        "recommended_threshold": recommended,
        "current_provisional_threshold": CLASSIFIER_CONFIDENCE_THRESHOLD,
        "label_distribution": distribution,
        # T-21 acceptance 4 — machine-checkable fields, not report.md prose.
        "run_timestamp_utc": run_started_at.isoformat(),
        "measured_sample_size": len(measured_tickets),
        "unmeasured_ticket_count": len(unmeasured_tickets),
        "unmeasured_ticket_ids": [t["id"] for t in unmeasured_tickets],
        "live_classifier_call_count": live_call_count,
        # Provenance, so an artifact can never silently read as a real
        # measurement. "live" is the only value a non-pytest run can produce
        # (see _resolve_llm_client / _running_under_pytest); a fixture run is
        # labelled as such right here in the machine-checkable metrics rather
        # than only in prose a reader might skip.
        "classifier_source": (
            "fixture" if os.environ.get(TEST_ONLY_FAKE_LLM_ENV_VAR) else "live"
        ),
        "predictions": {
            t["id"]: {
                "measured": predictions[t["id"]].measured,
                "expected_escalate": t["expected_escalate"],
                "predicted_escalate": predictions[t["id"]].escalate,
                "predicted_reasons": predictions[t["id"]].reasons,
            }
            for t in tickets
        },
    }
    (args.output_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))

    print(report_md)
    return 0 if approved else 1


if __name__ == "__main__":
    raise SystemExit(main())
