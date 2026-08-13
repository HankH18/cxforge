"""EmailAdapter harness backing the contract suite's email parametrization.

Not a test module itself (no ``test_`` prefix — pytest never collects it).
Unlike Zendesk, there is no separate real server to simulate over HTTP:
``EmailAdapter`` IS the in-memory fake (its own thread store + the
``InMemoryEmailTransport`` it records sends through), so seeding writes
directly into the same adapter under test via its test/dev seeding methods
(``seed_ticket`` / ``seed_comment`` / ``group_id_for`` — see
``email_adapter.py``), the same way ``ZendeskHarness`` seeds through
``FakeZendesk``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from helpdesk.email_adapter import DEFAULT_REQUESTER_EMAIL, EmailAdapter
from helpdesk.models import AuthorKind
from helpdesk.port import HelpdeskPort

from ._fake_zendesk import Seeded


@dataclass
class EmailHarness:
    """AdapterHarness for EmailAdapter (see conftest.py's ``AdapterHarness``
    Protocol). ``port`` and ``adapter`` are the same object; ``port`` is
    typed as the Protocol so generic contract tests only ever see
    HelpdeskPort's surface, and ``adapter`` is typed concretely so this
    harness (and adapter-specific tests) can reach the seeding/inspection
    methods that fall outside the Protocol — exactly the split
    ``ZendeskHarness`` makes between ``port`` and ``fake``."""

    port: HelpdeskPort
    adapter: EmailAdapter

    def seed_ticket(self, *, tags: list[str] | None = None) -> Seeded:
        ticket_id = self.adapter.seed_ticket(tags=tags)
        return Seeded(ticket_id=ticket_id, requester_email=DEFAULT_REQUESTER_EMAIL)

    def seed_comment(
        self,
        ticket_id: str,
        *,
        author: AuthorKind,
        text: str,
        public: bool = True,
        created_at: str | None = None,
    ) -> None:
        self.adapter.seed_comment(
            ticket_id, author=author, text=text, public=public, created_at=created_at
        )

    def group_id_for(self, ticket_id: str) -> str | None:
        return self.adapter.group_id_for(ticket_id)


def make_email_harness() -> Iterator[EmailHarness]:
    """Factory registered in conftest.py's ``ADAPTER_FACTORIES``. A
    generator (like ``make_zendesk_harness``) purely to match the
    ``Callable[[], Iterator[AdapterHarness]]`` factory shape — EmailAdapter
    needs no context-manager teardown since it never patches global state."""
    adapter = EmailAdapter()
    yield EmailHarness(port=adapter, adapter=adapter)
