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

The ``escalation_decider`` slot of ``AgentDeps`` is filled with a sentinel that
raises on any attribute access — neither node touches it, and if that ever
changes this provider fails loudly rather than measuring something else
quietly. ``port`` was that same sentinel until W2-B4/ADR-009 gave ``classify``
a ``fetch_requester_history`` call; it is now ``_NoHistoryPort``, which answers
that one method with "no prior contact" (true here — every case is one
synthetic message from a placeholder requester with no thread) and keeps the
raise-on-anything-else behaviour for the rest of the port.

TEST-ONLY escape hatch
----------------------
``EVALS_PROMPTFOO_FAKE_LLM_FOR_TESTS_ONLY`` substitutes canned schema responses
for ``LLMClient.structured`` so ``backend/tests/evals/test_promptfoo_provider.py``
can drive ``call_api`` — the REAL one, over the REAL nodes — inside
``-m "not live"``, which is network-free and runs with no API key. That test is
the thing that was missing: the ``_Unused("port")`` regression above killed all
19 classify cases and 810 offline tests stayed green, because nothing executed
this function.

Same two-signal guard as ``evals/route_accuracy.py`` and ``evals/report.py``:
the variable alone is not enough, the process must also be a real pytest process
(``PYTEST_VERSION``, which pytest sets for the whole process lifetime and which
a shell export cannot plausibly fake by accident). A stray export in a shell
running ``npx promptfoo eval`` is a loud error, not a green suite that never
reached the model. ``backend/tests/evals/test_promptfoo_provider.py::
test_fake_llm_hatch_is_refused_outside_a_pytest_process`` is the proof.
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
from pydantic import BaseModel  # noqa: E402

import agent  # noqa: E402, F401  (package init before escalation.* — see nodes docstring)
from agent import nodes  # noqa: E402
from agent.config import ANTHROPIC_MODEL  # noqa: E402
from agent.llm import AnthropicLLMClient, LLMClient  # noqa: E402
from agent.state import RunState  # noqa: E402
from data import KBChunk, RetrievedChunk  # noqa: E402
from evals.route_accuracy import _NoHistoryPort, _Unused  # noqa: E402
from helpdesk.models import Message, Ticket  # noqa: E402

_REPO_ROOT = _bootstrap.REPO_ROOT
_KB_DIR = _REPO_ROOT / "fixtures" / "kb"
_PLACEHOLDER_EMAIL = "promptfoo-harness@othram.invalid"
_PLACEHOLDER_TIMESTAMP = "2026-01-01T00:00:00+00:00"

TEST_ONLY_FAKE_LLM_ENV_VAR = "EVALS_PROMPTFOO_FAKE_LLM_FOR_TESTS_ONLY"


def _running_under_pytest() -> bool:
    """Second, independent signal that this really is a test process.

    Identical mechanism and identical reasoning to
    ``evals.route_accuracy._running_under_pytest``: ``PYTEST_VERSION`` is set by
    pytest for the whole process lifetime, so the canned-response hatch cannot be
    satisfied by a stray export in a shell running a real promptfoo eval.
    """
    return "PYTEST_VERSION" in os.environ


class _CannedLLMClient:
    """TEST-ONLY double — see the module docstring's escape-hatch section.

    The spec is ``{"<SchemaName>": {"default": {...}, "matches": [[needle, {...}]]}}``.
    ``matches`` is ordered; the first needle found anywhere in the assembled
    messages wins, else ``default`` — the same shape as
    ``evals.route_accuracy._FakeClassifyLLMClient``, so a test can prove which
    text actually reached the model call rather than only that one was made.

    A schema with no canned entry is an ERROR, never a silently-invented answer:
    if a node this provider drives grows a new ``structured`` call site, the
    offline acceptance test must be extended deliberately.
    """

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        canned = self._spec.get(schema.__name__)
        if canned is None:
            raise AssertionError(
                f"{TEST_ONLY_FAKE_LLM_ENV_VAR} carries no canned response for "
                f"{schema.__name__}. A node this provider drives now makes an LLM call the "
                "offline acceptance test does not know about — extend that test rather than "
                "widening the fake, or the suite stops measuring what it claims to."
            )
        blob = "\n".join(str(message.get("content", "")) for message in messages)
        for needle, payload in canned.get("matches", []):
            if str(needle) in blob:
                return schema(**payload)
        return schema(**canned["default"])


def _llm() -> LLMClient:
    canned = os.environ.get(TEST_ONLY_FAKE_LLM_ENV_VAR)
    if canned and not _running_under_pytest():
        raise RuntimeError(
            f"{TEST_ONLY_FAKE_LLM_ENV_VAR} is set, but this is not a pytest process. That "
            "variable substitutes a canned response for every model call; honouring it here "
            "would make promptfoo report on a suite that never reached the model. Unset it "
            "to run a real eval."
        )
    if canned:
        return _CannedLLMClient(json.loads(canned))

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
                # W2-B4 / ADR-009 gave `classify` a port call
                # (`fetch_requester_history`), so `_Unused("port")` here made
                # EVERY classify case in this suite raise the sentinel's
                # AssertionError — measured 2026-08-17: 19 errors, 5 passes.
                # `evals/route_accuracy.py` grew `_NoHistoryPort` for exactly
                # this and this provider was not updated with it; nothing in
                # backend/tests/evals/test_promptfoo_suite.py executes
                # `call_api`, so the offline suite stayed green through it.
                # `_NoHistoryPort` answers "no prior contact", which is the TRUE
                # answer here — a promptfoo case is a single synthetic message
                # with a placeholder requester and no thread — and still raises
                # on every OTHER port method, so the loud-failure property the
                # docstring claims is preserved.
                port=_NoHistoryPort(),  # type: ignore[arg-type]
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
