"""W1-E2 — DeepEval is settled, and stays settled.

``docs/DECISIONS.md`` ADR-013, verbatim: DeepEval "either does real work or it
comes out of ``pyproject.toml`` and out of SPEC. Leaving it declared and unused
is not an option." ``docs/STATE.md §6.11`` recorded the unsettled state: a
declared dependency with **zero** imports repo-wide.

E2's decision was **removal**, on four measured grounds (recorded in full in the
Track E hand-off, and summarised here because a future reader will hit this test
before they hit that document):

1. It was never imported. Nothing regresses.
2. The grounding suite's R9 enforcement is ``agent.grounding_guard`` — pure
   Python, no model call, chosen *precisely* so a groundedness judge "cannot buy
   its way past it" (that module's own docstring, recording the T-5 red-team
   finding). DeepEval's value is LLM-judged metrics: the second model opinion
   this design deliberately refuses to depend on.
3. Its default judge is OpenAI. Verified against deepeval 4.1.8:
   ``deepeval.metrics.utils.initialize_model`` falls through to ``OpenAIModel``
   when no model is passed, and this repo's ``.env`` still carries a pre-pivot
   ``OPENAI_API_KEY`` it would happily find. ADR-008/ADR-014's provider story is
   Anthropic for generation and Voyage for embeddings; quietly reintroducing
   OpenAI in the eval layer contradicts the one thing the pivot proved.
4. Its metrics make live judge calls and it ships opt-out telemetry
   (``DEEPEVAL_TELEMETRY_OPT_OUT``). The gated suite must stay offline and
   network-free.

ADR-013's requirement — a second, independent evidence stream — is met by the
promptfoo suite (``promptfooconfig.yaml``, ``evals/promptfoo/**``), which runs
the same adversarial grounding set against the live model and asserts it with
the shipped guard.

This test does not encode "DeepEval must be absent". It encodes the rule: a
declared dependency must be used. Anyone who wants DeepEval back can have it —
they just have to make it do work.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Distribution name -> the module name an import statement would use.
_WATCHED = {"deepeval": "deepeval"}

_SKIP_DIRS = {
    ".venv",
    ".git",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".claude",
}


def _declared_distributions() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text())
    specs: list[str] = list(data["project"].get("dependencies", []))
    for group in data.get("dependency-groups", {}).values():
        specs.extend(s for s in group if isinstance(s, str))
    return {re.split(r"[<>=!~\[; ]", spec, maxsplit=1)[0].strip().lower() for spec in specs}


def _imports_module(module: str) -> list[Path]:
    pattern = re.compile(rf"^\s*(?:import\s+{module}\b|from\s+{module}[\s.])", re.MULTILINE)
    hits: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.resolve() == Path(__file__).resolve():
            continue  # this module names it only in prose
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
            continue
        if pattern.search(text):
            hits.append(path.relative_to(REPO_ROOT))
    return hits


def test_no_declared_eval_dependency_is_left_unused() -> None:
    declared = _declared_distributions()
    for distribution, module in _WATCHED.items():
        if distribution not in declared:
            continue
        users = _imports_module(module)
        assert users, (
            f"{distribution!r} is declared in pyproject.toml but imported nowhere. "
            "ADR-013: it either does real work or it comes out of pyproject.toml and out "
            "of SPEC — leaving it declared and unused is not an option. See this module's "
            "docstring for why W1-E2 chose removal."
        )
