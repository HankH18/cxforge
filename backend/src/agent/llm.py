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
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel

from agent.config import ANTHROPIC_MAX_TOKENS, ANTHROPIC_MODEL

if TYPE_CHECKING:
    import anthropic


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
