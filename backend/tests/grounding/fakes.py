"""Test doubles for the grounding suite.

Intentionally mirrors ``backend/tests/graph/fakes.py`` field-for-field
rather than importing it: this ticket's scope is exactly
``backend/tests/graph/**`` and ``backend/tests/grounding/**`` (two
disjoint directories — no shared ``backend/tests/conftest.py`` is in
scope to hang a common import off of), and the file is small enough that
duplicating it beats a fragile cross-package relative import between two
sibling test directories that have no ``__init__.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from escalation.schemas import EscalationCall

ResponseOrFactory = BaseModel | Callable[[list[dict[str, Any]]], BaseModel]


@dataclass
class FakeLLMClient:
    """``agent.llm.LLMClient`` test double — see
    ``backend/tests/graph/fakes.py`` for the full docstring."""

    responses: dict[type[BaseModel], ResponseOrFactory]
    calls: list[tuple[type[BaseModel], list[dict[str, Any]]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Default the escalation classifier to a NON-escalating verdict.

        T-6 wired an unconditional classifier call into ``decide``, so every
        run now consults it. Without a registered response ``structured``
        raises, and ``run_classifier`` treats any exception as abstention —
        itself a pinned hard escalation trigger — which would silently turn
        these send-path grounding tests into escalation tests and stop them
        exercising the thing they exist to check. A test that wants to drive
        the classifier registers its own ``EscalationCall`` and overrides
        this. Mirrors ``backend/tests/graph/fakes.py``.
        """
        self.responses.setdefault(
            EscalationCall, EscalationCall(escalate=False, reasons=[], confidence=0.0)
        )

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
