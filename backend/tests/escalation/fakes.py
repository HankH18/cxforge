"""Test doubles for the escalation suite.

``FakeLLMClient`` mirrors ``backend/tests/graph/fakes.py`` field-for-field
(same rationale as ``backend/tests/grounding/fakes.py``: this ticket's
scope is ``backend/tests/escalation/**`` alone — a directory with no shared
import target in either sibling suite — so duplicating a ~20-line fake
beats a fragile cross-package import).

``RefusingLLMClient``, ``AbstainingLLMClient``, and ``RecordingHelpdeskPort``
are new to this suite:

- ``RefusingLLMClient`` raises on every call, so a test using it proves a
  decision was reached WITHOUT ever consulting the model — the strongest
  form of "hard rules are not overridable by the model" (not just "the
  classifier would have said no," but "the classifier was never asked").
  Its ``AssertionError`` is a programming-error tripwire, not a model
  failure — ``run_classifier`` (T-18) lets that propagate rather than
  absorbing it, so it can only be used where ``.structured`` is never
  actually expected to be called.
- ``AbstainingLLMClient`` raises a genuine, absorbable model-failure
  exception (``ValueError``, mirroring ``OpenAILLMClient``'s own refusal/
  truncation error) on every call — use it wherever a test wants
  ``run_classifier`` to actually run its except clause and abstain.
- ``RecordingHelpdeskPort`` is a minimal ``HelpdeskPort`` double that
  appends every call, in order, to one flat list — so a test can assert
  the exact SEQUENCE and completeness of an escalation run's port calls.
  ``helpdesk.email_adapter.EmailAdapter`` (reused by the graph/grounding
  suites) tracks each effect in its own place (``.transport.sent``,
  per-thread tags/status/group) with no unified ordered call log, which is
  exactly what this suite needs to assert on.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from helpdesk.models import EscalationGroup, Message, MessageRef, Ticket, TicketStatus

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

    def assert_consulted(self, schema: type[BaseModel]) -> None:
        """Assert ``schema`` was actually sent to ``.structured`` — not just
        defaulted away. Fails loudly if an expected call site (e.g. the
        escalation classifier) was silently never reached, catching a
        regression that removes/short-circuits it before it runs (T-18,
        guarding against a repeat of the R6 "classifier unreachable from
        the live graph" defect)."""
        called = [s for s, _ in self.calls]
        assert schema in called, (
            f"{schema.__name__} was never consulted (calls made: "
            f"{[s.__name__ for s in called]}) — an expected call site may "
            "have gone silently unreached."
        )


@dataclass
class RefusingLLMClient:
    """An ``LLMClient`` that raises on every ``.structured`` call — proves
    a decision was reached without ever consulting the model."""

    calls: int = 0

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        self.calls += 1
        raise AssertionError(
            f"RefusingLLMClient.structured({schema.__name__}) called — the caller should have "
            "short-circuited on a hard rule before ever consulting the model."
        )


@dataclass
class AbstainingLLMClient:
    """An ``LLMClient`` that raises a real "the model produced no usable
    output" failure on every ``.structured`` call — one of the exception
    types ``run_classifier`` narrows its ``except`` to (T-18), so a test
    using this exercises genuine abstention semantics.

    Deliberately distinct from ``RefusingLLMClient``, whose
    ``AssertionError`` is a *programming*-error tripwire ("you should never
    have called me") that ``run_classifier`` must now let propagate rather
    than absorb into abstention — so it can no longer stand in for "the
    classifier fails and abstains" once the except clause is narrowed."""

    calls: int = 0

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        self.calls += 1
        raise ValueError(f"{schema.__name__}: synthetic model refusal/truncation")


@dataclass
class RecordingHelpdeskPort:
    """A ``HelpdeskPort`` double recording every call, in order, as
    ``(method_name, kwargs)`` in ``self.calls`` — plus the same per-effect
    state (``sent``, ``notes``, ``tags``, ``status``, ``group``) a test
    might want to inspect directly without re-deriving it from the log."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    status: TicketStatus | None = None
    group: EscalationGroup | None = None
    _message_ids: int = 0

    def _next_message_id(self, kind: str) -> str:
        self._message_ids += 1
        return f"<{kind}-{self._message_ids}@recording-fake.local>"

    def fetch_ticket(self, ticket_id: str) -> Ticket:
        self.calls.append(("fetch_ticket", {"ticket_id": ticket_id}))
        return Ticket(
            id=ticket_id,
            subject="Recording fake ticket",
            requester_email="requester@example.com",
            status=self.status or "open",
            tags=list(self.tags),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def fetch_conversation(self, ticket_id: str) -> list[Message]:
        self.calls.append(("fetch_conversation", {"ticket_id": ticket_id}))
        return []

    def post_public_reply(self, ticket_id: str, html_body: str) -> MessageRef:
        self.calls.append(("post_public_reply", {"ticket_id": ticket_id, "html_body": html_body}))
        self.sent.append(html_body)
        return MessageRef(
            ticket_id=ticket_id, message_id=self._next_message_id("reply"), public=True
        )

    def post_internal_note(self, ticket_id: str, body: str) -> MessageRef:
        self.calls.append(("post_internal_note", {"ticket_id": ticket_id, "body": body}))
        self.notes.append(body)
        return MessageRef(
            ticket_id=ticket_id, message_id=self._next_message_id("note"), public=False
        )

    def add_tags(self, ticket_id: str, tags: list[str]) -> None:
        self.calls.append(("add_tags", {"ticket_id": ticket_id, "tags": list(tags)}))
        for tag in tags:
            if tag not in self.tags:
                self.tags.append(tag)

    def set_status(self, ticket_id: str, status: TicketStatus) -> None:
        self.calls.append(("set_status", {"ticket_id": ticket_id, "status": status}))
        self.status = status

    def assign_group(self, ticket_id: str, group: EscalationGroup) -> None:
        self.calls.append(("assign_group", {"ticket_id": ticket_id, "group": group}))
        self.group = group
