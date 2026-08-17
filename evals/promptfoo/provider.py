"""promptfoo custom provider — drives the SHIPPED agent nodes, not a copy.

W1-E1 / ADR-013. ``promptfooconfig.yaml`` points at this file, so every
promptfoo test case runs through the real ``backend/src/agent`` code path:

* ``suite: classify``  -> ``agent.nodes.classify``  (``agent.prompts.CLASSIFY_SYSTEM``,
  ``agent.schemas.Classification``)
* ``suite: kb_answer`` -> ``agent.nodes.compose``'s ``"kb"`` branch
  (``agent.prompts.KB_ANSWER_SYSTEM``, ``agent.schemas.KBAnswerDraft``)

WHY A CUSTOM PROVIDER AND NOT `anthropic:messages:...`
------------------------------------------------------
A promptfoo suite that pastes the prompt text into YAML measures the YAML, not
the product: editing ``backend/src/agent/prompts.py`` would leave it green.
Routing through the shipped node means the eval is bound to the code that ships
— degrade ``CLASSIFY_SYSTEM`` and this suite goes red. That is the acceptance
bar ``docs/BUILD-PLAN.md §3 Track E`` sets, and the only thing that makes a
second evidence stream worth having.

NO DATABASE REQUIRED
--------------------
``agent.nodes.kb_answer`` does the pgvector retrieval; ``compose`` only ever
reads ``state["retrieved_chunks"]``. So the KB context here is built directly
from ``fixtures/kb/*.md`` (the same corpus ``data.seed`` loads) and handed to
the real ``compose``. The retrieval step is deliberately out of scope — it is
measured by ``backend/tests/data`` and by W2-B; what this suite measures is
what the model does with the context it is given.

The ``port`` and ``escalation_decider`` slots of ``AgentDeps`` are filled with
a sentinel that raises on any attribute access — neither node touches them, and
if that ever changes this provider fails loudly rather than measuring something
else quietly.
"""

from __future__ import annotations

# _bootstrap must be imported before anything that needs a repo dependency,
# because it re-execs into the venv interpreter. Sorting these imports would
# silently break every promptfoo run on a machine whose default `python` is
# not this repo's — hence isort is switched off for this block.
# isort: off
import sys
from pathlib import Path

# promptfoo loads this file with importlib under a synthetic module name, so it
# is not part of a package and cannot use a relative import. Put its own
# directory on sys.path, then let _bootstrap re-exec into the repo venv and set
# up backend/src — see that module's docstring.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _bootstrap  # noqa: E402, F401  (MUST be first — re-execs the interpreter)

import json  # noqa: E402
import os  # noqa: E402
from typing import Any  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

import agent  # noqa: E402, F401  (package init before escalation.* — see nodes docstring)
from agent import nodes  # noqa: E402
from agent.config import ANTHROPIC_MODEL  # noqa: E402
from agent.llm import AnthropicLLMClient  # noqa: E402
from agent.state import RunState  # noqa: E402
from data import KBChunk, RetrievedChunk  # noqa: E402
from evals.route_accuracy import _Unused  # noqa: E402
from helpdesk.models import Message, Ticket  # noqa: E402

_REPO_ROOT = _bootstrap.REPO_ROOT
_KB_DIR = _REPO_ROOT / "fixtures" / "kb"
_PLACEHOLDER_EMAIL = "promptfoo-harness@othram.invalid"
_PLACEHOLDER_TIMESTAMP = "2026-01-01T00:00:00+00:00"


def _llm() -> AnthropicLLMClient:
    load_dotenv(dotenv_path=_REPO_ROOT / ".env", override=False)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set (process env or .env). This suite measures the "
            "real model on purpose — run it as `set -a; source .env; set +a; npx promptfoo eval`."
        )
    return AnthropicLLMClient(model=os.environ.get("CXFORGE_EVAL_MODEL", ANTHROPIC_MODEL))


def _config(ticket_id: str) -> Any:
    return {
        "configurable": {
            "ticket_id": ticket_id,
            "deps": nodes.AgentDeps(
                port=_Unused("port"),  # type: ignore[arg-type]
                llm=_llm(),
                escalation_decider=_Unused("escalation_decider"),  # type: ignore[arg-type]
            ),
        }
    }


def _conversation(message: str) -> tuple[Ticket, list[Message]]:
    ticket = Ticket(
        id="promptfoo-case",
        subject=message.splitlines()[0][:120] if message else "promptfoo case",
        requester_email=_PLACEHOLDER_EMAIL,
        status="open",
        tags=[],
        created_at=_PLACEHOLDER_TIMESTAMP,  # type: ignore[arg-type]
    )
    return ticket, [
        Message(
            id="promptfoo-case-body",
            author_kind="customer",
            text=message,
            public=True,
            created_at=_PLACEHOLDER_TIMESTAMP,  # type: ignore[arg-type]
        )
    ]


def _kb_chunks(doc_slugs: list[str]) -> list[RetrievedChunk]:
    """Real KB text from ``fixtures/kb/<slug>.md``, in the shape ``compose``
    expects. Fails loudly on a typo'd slug rather than composing over silence —
    an empty context would make every grounding assertion trivially pass."""
    chunks: list[RetrievedChunk] = []
    for index, slug in enumerate(doc_slugs):
        path = _KB_DIR / f"{slug}.md"
        if not path.exists():
            raise FileNotFoundError(
                f"promptfoo grounding case names kb doc {slug!r}, but {path} does not exist. "
                f"Available: {sorted(p.stem for p in _KB_DIR.glob('*.md'))}"
            )
        chunks.append(
            RetrievedChunk(
                chunk=KBChunk(id=index, doc_slug=slug, chunk_index=0, text=path.read_text()),
                score=1.0,
            )
        )
    return chunks


def _run_classify(message: str) -> dict[str, Any]:
    ticket, conversation = _conversation(message)
    state: RunState = {
        "ticket": ticket,
        "conversation": conversation,
        "tool_results": {},
        "actions": [],
    }
    update = nodes.classify(state, _config(ticket.id))
    return {
        "route": update["route"],
        "confidence": update.get("confidence"),
        "topic": update.get("topic"),
        "case_id": (update.get("tool_results") or {}).get("case_id_hint"),
    }


def _run_kb_answer(message: str, doc_slugs: list[str], topic: str) -> dict[str, Any]:
    ticket, conversation = _conversation(message)
    state: RunState = {
        "ticket": ticket,
        "conversation": conversation,
        "route": "kb",
        "topic": topic or message,
        "tool_results": {},
        "retrieved_chunks": _kb_chunks(doc_slugs),
        "actions": [],
    }
    update = nodes.compose(state, _config(ticket.id))
    return {"draft": update["draft"], "kb_docs": doc_slugs}


# Typographic characters the model reproduces from the KB fixtures, mapped to
# their ASCII equivalents. Two independent reasons, both measured against
# promptfoo 0.122.0 on 2026-08-16 and both silent failures if left alone:
#
# 1. promptfoo's python-shell transport CORRUPTS raw non-ASCII on the way back
#    to node. Returning `json.dumps(..., ensure_ascii=False)` turned the model's
#    "3-5 weeks. Well-preserved samples typically..." into
#    "3\to samples typically..." — a tab and several lost characters. Every
#    assertion downstream then grades mangled text.
# 2. Keeping the default `ensure_ascii=True` transports the text intact, but the
#    assertion then sees the literal escape `–`, so a `contains: "3-5"`
#    check fails on an answer that is in fact correct.
#
# Normalizing first and then serializing with the default escaping satisfies
# both: the payload is pure ASCII, so nothing is corrupted and nothing needs an
# escape. Anything not in this map still round-trips safely as `\uXXXX`.
_PUNCTUATION_TO_ASCII = {
    "–": "-",  # en dash
    "—": "--",  # em dash
    "‘": "'",  # left single quote
    "’": "'",  # right single quote / apostrophe
    "“": '"',  # left double quote
    "”": '"',  # right double quote
    "…": "...",  # ellipsis
    " ": " ",  # non-breaking space
}


def _ascii_safe(payload: dict[str, Any]) -> dict[str, Any]:
    def normalize(value: Any) -> Any:
        if isinstance(value, str):
            for source, target in _PUNCTUATION_TO_ASCII.items():
                value = value.replace(source, target)
            return value
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    return {key: normalize(value) for key, value in payload.items()}


def _split_slugs(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """promptfoo entry point. Returns the node's output as a JSON string so the
    assertions can address individual fields."""
    variables = (context or {}).get("vars", {}) or {}
    suite = variables.get("suite")
    message = variables.get("message") or prompt

    try:
        if suite == "classify":
            payload = _run_classify(message)
        elif suite == "kb_answer":
            payload = _run_kb_answer(
                message, _split_slugs(variables.get("kb_docs")), variables.get("topic", "")
            )
        else:
            raise ValueError(
                f"unknown suite {suite!r} — every promptfoo test case must set "
                "`suite: classify` or `suite: kb_answer`"
            )
    except Exception as exc:  # noqa: BLE001 — promptfoo wants the error as data
        return {"error": f"{type(exc).__name__}: {exc}"}

    return {"output": json.dumps(_ascii_safe(payload))}

# isort: on
