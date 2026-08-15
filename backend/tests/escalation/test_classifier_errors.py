"""T-18: ``escalation.classifier.run_classifier``'s except clause must
absorb only the API/timeout/refusal-shaped failures it actually intends to
handle — never a bare ``except Exception``, which converts ANY bug in this
path into ``None`` (DESIGN's pinned "classifier abstention" hard escalation
trigger, see ``escalation.rules.is_classifier_abstention``), silently and
unlogged.

Four cases:

1. A programming error (the ticket's own named accident: an unregistered
   schema on a test double) PROPAGATES rather than being absorbed.
2. A second, unrelated programming-error TYPE also propagates — proving the
   fix narrows by exception *category*, not by patching around one accident.
3. A genuine OpenAI SDK API/connection failure is absorbed to ``None``, with
   a warning naming the exception type.
4. A genuine refusal/truncation (``OpenAILLMClient``'s own ``ValueError``,
   see ``agent/llm.py``) is absorbed to ``None``, with a warning naming the
   exception type.

Together, 1-2 and 3-4 also prove abstention semantics are unchanged: a
refusal or unparseable verdict still abstains (3, 4), while a bug is no
longer disguised as one (1, 2).
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from escalation.classifier import run_classifier

from .conftest import make_conversation
from .fakes import FakeLLMClient

_CONVERSATION = make_conversation("What's going on with my case?")
_TOPIC = "ambiguous request"


class _BuggyLLMClient:
    """A second, unrelated programming-error type — not
    ``FakeLLMClient``'s unregistered-schema ``AssertionError`` — to prove
    the narrowed except propagates by *category* (anything that isn't
    ``anthropic.AnthropicError``/``ValueError``), not just that one accident."""

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        raise TypeError("boom: a genuine bug, not a model-shaped failure")


class _APIFailingLLMClient:
    """Raises a real Anthropic SDK exception — exactly the shape
    ``AnthropicLLMClient.structured`` (``agent/llm.py``) would raise if the
    underlying ``messages.parse`` call failed.

    Updated with the authorised OpenAI -> Anthropic pivot. Only the provider's
    exception CLASS moved; T-18's contract is untouched — a transport/API
    failure is still absorbed to abstention, and a programming error still
    propagates (``_BuggyLLMClient`` above)."""

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        raise anthropic.APIConnectionError(request=request)


class _RefusingParseLLMClient:
    """Mirrors ``AnthropicLLMClient.structured``'s own behavior on a refusal
    or truncated response: a bare ``ValueError``, not an SDK exception."""

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        raise ValueError(
            "OpenAI structured output for EscalationCall returned no parsed result "
            "(refusal or truncation) — refusing to guess at a value."
        )


# -- 1. programming error (unregistered schema) propagates -------------------


def test_unregistered_schema_programming_error_propagates() -> None:
    """``FakeLLMClient`` with no ``EscalationCall`` response registered
    raises ``AssertionError`` — the fakes' own tripwire for "an unexpected
    call site was reached." Under the OLD bare ``except Exception``, this
    was silently swallowed into abstention. It must now escape."""
    llm = FakeLLMClient(responses={})
    with pytest.raises(AssertionError):
        run_classifier(llm, conversation=_CONVERSATION, topic=_TOPIC)


# -- 2. a different programming-error type also propagates -------------------


def test_arbitrary_programming_error_propagates() -> None:
    with pytest.raises(TypeError):
        run_classifier(_BuggyLLMClient(), conversation=_CONVERSATION, topic=_TOPIC)


# -- 3. genuine OpenAI API/connection failure absorbed -> None, logged -------


def test_provider_api_error_is_absorbed_to_none_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        result = run_classifier(
            _APIFailingLLMClient(), conversation=_CONVERSATION, topic=_TOPIC
        )
    assert result is None
    assert any("APIConnectionError" in record.getMessage() for record in caplog.records)


# -- 4. genuine refusal/truncation absorbed -> None, logged ------------------


def test_refusal_value_error_is_absorbed_to_none_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        result = run_classifier(
            _RefusingParseLLMClient(), conversation=_CONVERSATION, topic=_TOPIC
        )
    assert result is None
    assert any("ValueError" in record.getMessage() for record in caplog.records)
