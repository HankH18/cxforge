"""Shared fixtures for the HelpdeskPort contract suite (DESIGN §HelpdeskPort).

``adapter_harness`` is the single parametrization point every test in
test_port_contract.py draws its adapter from. Adding a new adapter is a
ONE-LINE change to ``ADAPTER_FACTORIES`` below — T-3 adds:

    "email": make_email_harness,

(plus the matching import) and nothing else in this suite needs to change:
every test in test_port_contract.py is written against ``AdapterHarness``
and the ``HelpdeskPort`` Protocol only, never against a concrete adapter.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Protocol

import pytest

from helpdesk.models import AuthorKind
from helpdesk.port import HelpdeskPort

from ._fake_zendesk import Seeded, ZendeskHarness, make_zendesk_harness


class AdapterHarness(Protocol):
    """What a contract test may know about an adapter beyond the Protocol
    itself: how to seed a starting state, and how to read back the one
    write (`assign_group`'s target) that `Ticket`/`Message` don't expose.
    Every adapter factory below returns something satisfying this."""

    port: HelpdeskPort

    def seed_ticket(self, *, tags: list[str] | None = None) -> Seeded: ...

    def seed_comment(
        self,
        ticket_id: str,
        *,
        author: AuthorKind,
        text: str,
        public: bool = True,
        created_at: str | None = None,
    ) -> None: ...

    def group_id_for(self, ticket_id: str) -> str | None: ...


# One factory per adapter under test, keyed by a short id used as the
# fixture's parametrize id (shows up in `pytest -v` as
# `test_name[zendesk]`, `test_name[email]`, ...).
ADAPTER_FACTORIES: dict[str, Callable[[], Iterator[AdapterHarness]]] = {
    "zendesk": make_zendesk_harness,
}


@pytest.fixture(params=list(ADAPTER_FACTORIES))
def adapter_harness(request: pytest.FixtureRequest) -> Iterator[AdapterHarness]:
    factory = ADAPTER_FACTORIES[request.param]
    yield from factory()


@pytest.fixture
def zendesk_harness() -> Iterator[ZendeskHarness]:
    """Zendesk-only harness (not parametrized) for tests in
    test_zendesk_adapter.py that assert HTTP-level behavior — retry/backoff,
    typed errors on non-retryable responses — which has no equivalent in a
    non-HTTP adapter and so cannot live in the generic parametrized suite.
    """
    yield from make_zendesk_harness()


# Re-exported so test modules only need `from .conftest import ...`.
__all__ = ["AdapterHarness", "Seeded", "ZendeskHarness", "adapter_harness", "zendesk_harness"]
