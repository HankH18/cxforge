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

ResponseOrFactory = BaseModel | Callable[[list[dict[str, Any]]], BaseModel]


@dataclass
class FakeLLMClient:
    """``agent.llm.LLMClient`` test double — see
    ``backend/tests/graph/fakes.py`` for the full docstring."""

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
