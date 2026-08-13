"""LLMClient isolation layer — DESIGN §LLMClient, pinned verbatim:

```python
class LLMClient(Protocol):
    def structured(self, schema: type[BaseModel], messages: list[dict],
                   temperature: float = 0.0) -> BaseModel: ...
```

Every model call anywhere in the graph goes through this Protocol — no
node ever imports ``openai`` directly. ``OpenAILLMClient`` is the
production implementation; it is constructed lazily (see below) so that
merely importing this module, or even instantiating
``OpenAILLMClient(...)``, never touches the network or requires
``OPENAI_API_KEY``.

This environment has no ``OPENAI_API_KEY``, so ``OpenAILLMClient`` is
implemented here but has never been exercised live — every graph/grounding
test in this ticket runs against a fake ``LLMClient`` (see
``backend/tests/graph/fakes.py``) that returns canned structured outputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel

from agent.config import OPENAI_MODEL

if TYPE_CHECKING:
    import openai


@runtime_checkable
class LLMClient(Protocol):
    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel: ...


class OpenAILLMClient:
    """Structured-output OpenAI implementation of ``LLMClient``.

    Uses ``chat.completions.parse`` — the SDK's strict, JSON-schema-backed
    structured-output path — so a response is guaranteed to validate
    against ``schema`` before ``.structured()`` ever returns one.

    The underlying ``openai.OpenAI`` client is created lazily, on the first
    ``.structured()`` call, not in ``__init__``: constructing
    ``OpenAILLMClient()`` (and importing this module) never requires
    ``OPENAI_API_KEY`` to be set. Only an actual live call would raise if
    no key is available — never exercised in this ticket's test suite.
    """

    def __init__(self, *, api_key: str | None = None, model: str = OPENAI_MODEL) -> None:
        self._api_key = api_key
        self._model = model
        self._client: openai.OpenAI | None = None

    def _get_client(self) -> openai.OpenAI:
        if self._client is None:
            import openai as openai_module

            self._client = openai_module.OpenAI(api_key=self._api_key)
        return self._client

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        client = self._get_client()
        completion = client.chat.completions.parse(
            model=self._model,
            messages=messages,  # type: ignore[arg-type]
            response_format=schema,
            temperature=temperature,
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise ValueError(
                f"OpenAI structured output for {schema.__name__} returned no parsed result "
                f"(refusal or truncation) — refusing to guess at a value."
            )
        return parsed
