"""Test doubles shared by the graph suite (and, via a thin re-export, the
grounding suite — see ``backend/tests/grounding/fakes.py``).

Not a test module itself (no ``test_`` prefix — pytest never collects it),
mirroring ``backend/tests/contract/_fake_zendesk.py``'s convention.

``FakeLLMClient`` satisfies ``agent.llm.LLMClient`` structurally (it's a
``Protocol``): tests register one canned response (or a callable that
inspects the messages and returns one) per Pydantic schema class, so a
single fake instance can back an entire graph run that calls
``.structured`` several times with different schemas (classify, a
permission match, a KB answer, a groundedness judgment). An unregistered
schema raises ``AssertionError`` rather than returning something made up —
a test that reaches a model call it didn't anticipate should fail loudly,
not silently drift.

The real ``HelpdeskPort`` fake is ``helpdesk.email_adapter.EmailAdapter``
(T-3) reused as-is: it's already a fully in-memory, contract-suite-tested
recorder (``seed_ticket``/``seed_comment`` to set up a run,
``.transport.sent`` / ``._threads[id]`` to inspect exactly which port
calls happened) — building a second one here would just be a worse copy.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

ResponseOrFactory = BaseModel | Callable[[list[dict[str, Any]]], BaseModel]


@dataclass
class FakeLLMClient:
    """``agent.llm.LLMClient`` test double. ``responses`` maps a schema
    class to either a fixed instance of it or a callable that receives the
    messages passed to ``.structured`` and returns one — the callable form
    lets a test vary its canned answer based on prompt content (e.g. a
    groundedness judge that scores differently per draft) without needing
    a second fake."""

    responses: dict[type[BaseModel], ResponseOrFactory]
    calls: list[tuple[type[BaseModel], list[dict[str, Any]]]] = field(default_factory=list)

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        self.calls.append((schema, messages))
        response = self.responses.get(schema)
        if response is None:
            raise AssertionError(
                f"FakeLLMClient has no canned response registered for {schema.__name__} "
                f"(registered: {[s.__name__ for s in self.responses]})"
            )
        if isinstance(response, BaseModel):
            return response
        return response(messages)
