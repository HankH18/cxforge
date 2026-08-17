"""LLMClient isolation layer — DESIGN §LLMClient, pinned verbatim:

```python
class LLMClient(Protocol):
    def structured(self, schema: type[BaseModel], messages: list[dict],
                   temperature: float = 0.0) -> BaseModel: ...
```

Every model call anywhere in the graph goes through this Protocol — no node
ever imports a provider SDK directly. ``AnthropicLLMClient`` is the
production implementation; it is constructed lazily (see below) so that
merely importing this module, or even instantiating
``AnthropicLLMClient(...)``, never touches the network or requires
``ANTHROPIC_API_KEY``.

PROVIDER PIVOT (authorised by the project owner): this layer was originally
written against OpenAI's ``chat.completions.parse``. It now targets the
Anthropic Messages API. The Protocol signature above is unchanged, so no
caller, node, or test double moved — that isolation is exactly what the
seam existed for.

Three Anthropic-specific details are load-bearing here, and each one is a
place where a habit carried over from the OpenAI path would produce a
runtime 400 rather than a type error:

* **``system`` is a top-level request parameter, not a message role.** The
  callers in this repo build OpenAI-shaped message lists that lead with a
  ``{"role": "system", ...}`` entry, so ``structured()`` lifts those out and
  passes them as ``system=``. Leaving a system-role entry in ``messages``
  is rejected by the API.
* **Sampling parameters are rejected.** ``temperature`` (and ``top_p`` /
  ``top_k``) return a 400 on ``claude-opus-5``. The Protocol's
  ``temperature`` argument is therefore accepted and deliberately ignored —
  it is kept only because DESIGN pins the signature verbatim, and dropping
  it would break every existing call site for no benefit.
* **Thinking is on by default and shares the ``max_tokens`` budget** with
  the response itself, so ``max_tokens`` is sized with real headroom. Too
  small a budget truncates the answer mid-structure rather than erroring.

Structured output uses ``client.messages.parse(..., output_format=schema)``,
the SDK's schema-validated path, so a response is guaranteed to validate
against ``schema`` before ``.structured()`` ever returns it.

W2-C1 (ADR-006) adds a second vendor SDK behind this same seam: Langfuse.
It lives here for the reason stated in the first paragraph — this module is
where ``agent/**`` is allowed to import a provider SDK, and nowhere else is
— so ``agent.nodes`` hands ``emit_trace`` plain dicts and never learns that
``langfuse`` exists. See the "Langfuse tracing seam" section at the bottom.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel

from agent.config import ANTHROPIC_MAX_TOKENS, ANTHROPIC_MODEL

if TYPE_CHECKING:
    import anthropic

logger = logging.getLogger(__name__)


@runtime_checkable
class LLMClient(Protocol):
    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel: ...


class AnthropicLLMClient:
    """Structured-output Anthropic implementation of ``LLMClient``.

    The underlying ``anthropic.Anthropic`` client is created lazily, on the
    first ``.structured()`` call, not in ``__init__``: constructing
    ``AnthropicLLMClient()`` (and importing this module) never requires
    ``ANTHROPIC_API_KEY`` to be set, so the whole graph stays importable and
    unit-testable with no credential present. Only an actual live call
    raises if no key is available.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = ANTHROPIC_MODEL,
        max_tokens: int = ANTHROPIC_MAX_TOKENS,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._client: anthropic.Anthropic | None = None

    def _get_client(self) -> anthropic.Anthropic:
        if self._client is None:
            import anthropic as anthropic_module

            # api_key=None lets the SDK resolve ANTHROPIC_API_KEY (or an
            # `ant auth login` profile) from the environment itself.
            self._client = anthropic_module.Anthropic(api_key=self._api_key)
        return self._client

    @staticmethod
    def _split_system(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Lift OpenAI-shaped ``system`` turns out into Anthropic's top-level
        ``system`` parameter.

        Anthropic has no ``system`` message role in this position — a system
        entry left inside ``messages`` is rejected. Multiple system turns are
        joined in order rather than silently dropping all but one, so a caller
        that builds its prompt in pieces keeps every piece.
        """
        system_parts = [
            str(m.get("content", "")) for m in messages if m.get("role") == "system"
        ]
        turns = [m for m in messages if m.get("role") != "system"]
        return ("\n\n".join(p for p in system_parts if p) or None), turns

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        # `temperature` is intentionally unused — see the module docstring.
        del temperature
        client = self._get_client()
        system, turns = self._split_system(messages)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": turns,
            "output_format": schema,
        }
        if system is not None:
            kwargs["system"] = system

        response = client.messages.parse(**kwargs)
        parsed = response.parsed_output
        if parsed is None:
            # Mirrors the refusal/truncation contract the escalation
            # classifier already absorbs (see escalation/classifier.py and
            # escalation/rules.py's abstention docstring): a ValueError here
            # means "no usable verdict", which is a hard escalation trigger,
            # never a silent pass.
            raise ValueError(
                f"Anthropic structured output for {schema.__name__} returned no parsed "
                f"result (refusal or truncation) — refusing to guess at a value."
            )
        return parsed


# ==========================================================================
# Langfuse tracing seam (ADR-006 / W2-C1)
# ==========================================================================
#
# `agent.nodes.act` mints one ``trace_id`` per run and persists it on
# ``runs.trace_id``; the portal turns that id into a link
# (``portal.service._trace_url``). Until W2-C1 nothing ever reported the id
# to Langfuse, so every link in the feed 404'd — `docs/STATE.md §6.3`.
#
# Two properties are load-bearing and are tested individually in
# `backend/tests/graph/test_tracing.py`:
#
# 1. **The trace is keyed on the id `act` already minted** (BUILD-PLAN §1.6).
#    A second id would resolve to a different (empty) trace and the feed's
#    link would still be dead. `trace_context={"trace_id": ...}` is how a
#    caller-owned id is attached; ``uuid.uuid4().hex`` is 32 lowercase hex
#    characters, which is exactly a W3C/OTEL trace id, so no translation is
#    needed. Verified end to end by reading the trace back from the API.
#
# 2. **It degrades to a genuine no-op**, so the offline suite stays offline.
#    "No-op" here means the `langfuse` package is never even imported and no
#    client object exists — not "a client that quietly drops spans". The
#    SDK's own behaviour with absent keys is the weaker second thing: it
#    installs a `NoOpTracer` but still constructs a client and logs a
#    warning per process. Both gates below are checked *before* anything
#    from `langfuse` is touched.
#
# Nothing here may raise into a run. A trace is diagnostics; a customer
# reply is the product. Every failure path logs and returns ``False``.


@dataclass(frozen=True)
class TraceSpan:
    """One observation to hang under a run's trace.

    Deliberately a plain data holder over plain values: `agent.nodes` builds
    these out of `RunState` and never touches a Langfuse type, which is what
    lets the whole span-construction path be tested with no SDK involved.

    ``kind`` is a Langfuse observation type (``span``, ``tool``,
    ``retriever``, ``evaluator``, ``generation``, ``chain``, ...). It only
    changes the icon and grouping in the UI, never the data.
    """

    name: str
    kind: str = "span"
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] | None = None


def _running_under_pytest() -> bool:
    """The same structural gate `backend/src/main.py`, `backend/src/worker/
    main.py` and `backend/src/data/db.py` already use, for the same reason.

    A developer who runs the suite as the docs describe deploys —
    ``set -a; source .env; set +a; uv run pytest`` — has real `pk-lf-`/
    `sk-lf-` keys in the environment. Without this gate every graph test
    would ship its fixture runs to the real `cxforge` project, and the
    offline suite would be making network calls to a vendor. Checking the
    keys alone is NOT enough to keep that promise, which is why this is a
    second, independent gate rather than a comment.

    Tests that need emission to happen monkeypatch this function; that is
    the only way it is ever bypassed.
    """
    return "PYTEST_VERSION" in os.environ


def langfuse_configured() -> bool:
    """Both halves of the key pair present and non-empty.

    In Langfuse the key **pair is the project pointer** (`docs/STATE.md
    §3.1`) and Langfuse Cloud authenticates on the secret alone — a wrong
    public key still returns 200, and `auth_check()` returns ``True``
    against a garbage public key. So this function is a check that we were
    *configured*, and deliberately not a claim that the pair is *correct*.
    Nothing in this module treats `auth_check()` as evidence of anything.
    """
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY")) and bool(
        os.environ.get("LANGFUSE_SECRET_KEY")
    )


def _new_client() -> Any:
    """The one place `langfuse` is imported, and only ever after both gates
    above have passed. Keys and host come from `LANGFUSE_PUBLIC_KEY` /
    `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST`, resolved by the SDK itself."""
    from langfuse import Langfuse

    return Langfuse()


_client: Any | None = None
_client_lock = threading.Lock()


def reset_tracing() -> None:
    """Drop the cached client. For tests, and for a process that changes its
    Langfuse configuration mid-life (nothing in production does)."""
    global _client
    with _client_lock:
        _client = None


def _tracing_client() -> Any | None:
    """The client, or ``None`` when tracing must not happen.

    Cached because a `Langfuse()` builds an OTel tracer provider and starts
    background exporter threads; arq runs jobs concurrently, so the build is
    behind a lock. The cache is keyed on nothing: a process that changed its
    keys after the first run would keep using the first client. Call
    ``reset_tracing()`` if that ever becomes real.
    """
    global _client
    if not langfuse_configured():
        return None
    if _running_under_pytest():
        return None
    with _client_lock:
        if _client is None:
            _client = _new_client()
        return _client


def _jsonable(value: Any) -> Any:
    """Normalise to JSON-safe values before they leave this process.

    Langfuse has its own serializer, but the graph's payloads carry
    `datetime.date` (`data.Case.stage_entered_at`) and Pydantic models, and
    a serializer surprise here would silently truncate the one artefact the
    zero-hallucination story is told with. Doing it explicitly means the
    span-content tests assert on exactly the bytes the SDK is handed.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def emit_trace(
    *,
    trace_id: str,
    name: str,
    input: Any = None,
    output: Any = None,
    metadata: dict[str, Any] | None = None,
    spans: tuple[TraceSpan, ...] | list[TraceSpan] = (),
) -> bool:
    """Emit one finished trace under ``trace_id``. Returns whether it went.

    ``False`` means "tracing is off or failed", never an exception: a broken
    Langfuse must not cost a customer a reply. The flush is synchronous
    because a run is a handful of spans a few seconds apart, and an arq
    worker restarting mid-batch would otherwise lose exactly the traces
    someone is watching for.

    ``_tracing_client()`` is called INSIDE the guard, not before it. That is
    not tidiness — it is a defect this module had until an offline probe
    tripped it: `_new_client` runs ``from langfuse import Langfuse``, and an
    ImportError there (a broken wheel, a version conflict, an image built
    without the extra) sailed straight out of `act` and killed a run whose
    reply had already been posted. Client construction is as failable as
    everything after it.
    """
    try:
        client = _tracing_client()
        if client is None:
            return False
        root = client.start_observation(
            trace_context={"trace_id": trace_id},
            name=name,
            as_type="chain",
            input=_jsonable(input),
            output=_jsonable(output),
            metadata=_jsonable(metadata),
        )
        for span in spans:
            child = root.start_observation(
                name=span.name,
                as_type=span.kind,
                input=_jsonable(span.input),
                output=_jsonable(span.output),
                metadata=_jsonable(span.metadata),
            )
            child.end()
        root.end()
        client.flush()
    except Exception:
        logger.warning(
            "langfuse trace %s could not be emitted; the run itself is unaffected",
            trace_id,
            exc_info=True,
        )
        return False
    return True
