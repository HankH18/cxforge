"""T-21 acceptance 3: the report and ``escalation.engine.EscalationEngine``
can never diverge — ``evals/report.py`` has no escalation decision logic of
its own. This is the structural proof, not a comment.

Drives ``evals/report.py`` as a real subprocess (see ``test_report.py``'s
own docstring for why this suite stays black-box rather than importing
``evals.report`` in-process — ``evals`` lives outside ``backend/src``,
which is the only path ``uv run mypy backend`` resolves) using its
TEST-ONLY fixed-verdict escape hatch, reads the report's own predicted
``escalate`` for one classifier-tier ticket back out of ``metrics.json``,
and independently reconstructs the SAME decision by calling
``EscalationEngine.evaluate`` directly, in-process, with an equivalent fake
``LLMClient`` and the report's own ``recommended_threshold``.

Run TWICE, with two verdicts chosen so the engine's decision flips
unconditionally (``escalate=True`` vs ``escalate=False`` — not merely a
confidence/threshold nudge, which the F1-maximizing threshold sweep can
itself absorb by re-choosing the threshold around whatever confidence value
it's handed): if ``evals/report.py`` ever grew its own escalation logic
that happened to agree with the engine only by coincidence, this would
still catch it, because the direct engine call and the report's own
prediction are computed by two entirely separate code paths that must stay
in lockstep across a genuine behavior change, not just once.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# Import order matters here: `agent` (the package) must finish initializing
# BEFORE `escalation.engine` is ever touched, or the two modules' mutual
# imports (agent.graph imports escalation.engine; escalation.engine imports
# agent.escalation_seam) deadlock into a circular ImportError depending on
# which one some earlier-collected test file happened to import first. This
# is a pre-existing repo-wide quirk (backend/tests/escalation/
# test_act_sequence.py, collected before test_combinator.py, exists in part
# because `from agent import nodes, store, templates` there resolves it for
# every escalation test that follows) — not something T-21 introduced.
# Importing the package explicitly, first, makes this file collection-order
# independent instead of accidentally relying on a sibling module.
import agent  # noqa: F401
from escalation.engine import EscalationEngine
from escalation.schemas import EscalationCall
from helpdesk.models import Message, Ticket

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_CASES = REPO_ROOT / "fixtures" / "cases.yaml"

# One threshold-invariant anchor (a real hard rule — always TP, at every
# threshold) plus one classifier-tier ticket whose body trips neither the
# billing nor the human_request regex, so it genuinely reaches
# EscalationEngine.evaluate()'s classifier call.
CLASSIFIER_TIER_ID = "t-classifier-tier"
CLASSIFIER_TIER_BODY = "This is the third time I've asked and still no answer, very frustrating."

LABELED_SET = f"""\
approval:
  status: APPROVED
  approved_by: "Test Reviewer"
  approved_date: "2026-08-13"
meta: {{}}
tickets:
  - id: t-anchor-billing
    subject: "Billing dispute"
    body: "I was charged twice for my extraction fee, this is a billing error."
    expected_route: escalate
    expected_escalate: true
    expected_reasons: [billing]
  - id: {CLASSIFIER_TIER_ID}
    subject: "Frustrated customer"
    body: "{CLASSIFIER_TIER_BODY}"
    expected_route: escalate
    expected_escalate: true
    expected_reasons: [frustration]
"""

_PLACEHOLDER_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeLLMClient:
    """Minimal ``agent.llm.LLMClient`` double returning one fixed verdict —
    equivalent to ``evals.report._FixedVerdictLLMClient``, reimplemented
    here (not imported — see module docstring on why this file cannot
    import ``evals.report``) so the direct engine call below is genuinely
    independent of the report's own code."""

    def __init__(self, verdict: EscalationCall) -> None:
        self._verdict = verdict

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        assert schema is EscalationCall, schema
        return self._verdict


def _synthetic_repo(root: Path) -> Path:
    (root / "evals").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / "evals" / "__init__.py", root / "evals" / "__init__.py")
    shutil.copy(REPO_ROOT / "evals" / "report.py", root / "evals" / "report.py")
    (root / "evals" / "labeled_set.yaml").write_text(LABELED_SET)
    return root


def _run_report_with_verdict(
    repo: Path, output_dir: Path, verdict: dict[str, Any]
) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "backend" / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    env["EVALS_REPORT_FAKE_LLM_FOR_TESTS_ONLY"] = json.dumps(verdict)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.report",
            "--cases",
            str(REAL_CASES),
            "--output-dir",
            str(output_dir),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    metrics: dict[str, Any] = json.loads((output_dir / "metrics.json").read_text())
    return metrics


def _direct_engine_decision(verdict: dict[str, Any], threshold: float) -> bool:
    """The SAME decision, computed by calling ``EscalationEngine.evaluate``
    directly — nothing routed through ``evals/report.py``."""
    engine = EscalationEngine(
        llm=_FakeLLMClient(EscalationCall(**verdict)),
        threshold=threshold,
    )
    ticket = Ticket(
        id=CLASSIFIER_TIER_ID,
        subject="Frustrated customer",
        requester_email="direct-check@example.invalid",
        status="open",
        tags=[],
        created_at=_PLACEHOLDER_TIMESTAMP,
    )
    conversation = [
        Message(
            id="m1",
            author_kind="customer",
            text=CLASSIFIER_TIER_BODY,
            public=True,
            created_at=_PLACEHOLDER_TIMESTAMP,
        )
    ]
    decision = engine.evaluate(
        ticket=ticket, conversation=conversation, topic="Frustrated customer", tool_results={}
    )
    return decision.escalate


def test_report_prediction_matches_a_direct_engine_call_and_is_not_vacuous(tmp_path: Path) -> None:
    repo = _synthetic_repo(tmp_path / "repo")

    # escalate=True unconditionally fires the classifier tier (any
    # confidence, any threshold the sweep might pick).
    should_escalate = {"escalate": True, "reasons": ["frustration"], "confidence": 0.9}
    metrics_yes = _run_report_with_verdict(repo, tmp_path / "out-yes", should_escalate)
    predicted_yes = metrics_yes["predictions"][CLASSIFIER_TIER_ID]["predicted_escalate"]
    threshold_yes = metrics_yes["recommended_threshold"]
    assert predicted_yes == _direct_engine_decision(should_escalate, threshold_yes)

    # escalate=False unconditionally suppresses it, REGARDLESS of confidence
    # or threshold (EscalationEngine.evaluate's own `call.escalate and
    # call.confidence >= threshold` short-circuits on `call.escalate`
    # first) — deliberately not a confidence/threshold nudge, which the
    # F1-maximizing sweep could itself neutralize by re-choosing the
    # threshold around whatever confidence it's handed.
    should_not_escalate = {"escalate": False, "reasons": [], "confidence": 0.9}
    metrics_no = _run_report_with_verdict(repo, tmp_path / "out-no", should_not_escalate)
    predicted_no = metrics_no["predictions"][CLASSIFIER_TIER_ID]["predicted_escalate"]
    threshold_no = metrics_no["recommended_threshold"]
    assert predicted_no == _direct_engine_decision(should_not_escalate, threshold_no)

    # Not vacuous: the report's own prediction genuinely flipped between the
    # two runs — this could not pass if evals/report.py ignored the engine
    # and returned a constant.
    assert predicted_yes is True
    assert predicted_no is False
