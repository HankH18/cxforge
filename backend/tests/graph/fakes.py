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

``EscalationCall`` is the one exception, defaulted rather than left to
raise: T-6 wires ``agent.nodes.decide`` to call the escalation classifier
(``escalation.classifier.run_classifier``, one ``LLMClient.structured``
call against this exact schema) unconditionally for every run that reaches
``decide`` without already having escalated on a hard rule — see
``agent.nodes.decide``'s docstring. Every canonical scenario in this suite
was written before that call site existed, so none of them registers an
``EscalationCall`` response; without a default, every one of them would now
hit the "no canned response" ``AssertionError`` — not because anything
about the scenario changed, but purely because a new, incidental model call
was added to a path they don't care about. ``__post_init__`` below
pre-registers a NON-escalating verdict (``escalate=False``) as that
default, via ``dict.setdefault`` — so an existing test's
``responses={...}`` dict, and the route/draft/port assertions built on top
of it, keep meaning exactly what they meant before, while a test that
specifically wants to exercise the classifier (an escalating or
below-threshold verdict) can still override it by passing its own
``EscalationCall`` entry in the constructor call, same as any other schema.

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

from escalation.schemas import EscalationCall

ResponseOrFactory = BaseModel | Callable[[list[dict[str, Any]]], BaseModel]

# The safe, non-escalating default every ``FakeLLMClient`` registers for
# ``EscalationCall`` unless a test overrides it — see the module docstring.
DEFAULT_ESCALATION_CALL = EscalationCall(escalate=False, reasons=[], confidence=0.0)


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

    def __post_init__(self) -> None:
        self.responses.setdefault(EscalationCall, DEFAULT_ESCALATION_CALL)

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

    def assert_consulted(self, schema: type[BaseModel]) -> None:
        """Assert ``schema`` was actually sent to ``.structured`` — not just
        defaulted away (the ``EscalationCall`` default set in
        ``__post_init__`` above makes that distinction easy to lose). Fails
        loudly if an expected call site (e.g. the escalation classifier)
        was silently never reached, catching a regression that removes/
        short-circuits it before it runs (T-18, guarding against a repeat
        of the R6 "classifier unreachable from the live graph" defect)."""
        called = [s for s, _ in self.calls]
        assert schema in called, (
            f"{schema.__name__} was never consulted (calls made: "
            f"{[s.__name__ for s in called]}) — an expected call site may "
            "have gone silently unreached."
        )
