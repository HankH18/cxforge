"""Run-scoped collaborators the portal endpoints need, injected via FastAPI
``Depends`` — the same swap-a-fake-for-the-real-thing seam
``agent.nodes.AgentDeps`` gives the graph (see that module), expressed
FastAPI's way so tests use ``app.dependency_overrides`` instead of
monkeypatching a module global.
"""

from __future__ import annotations

from helpdesk.port import HelpdeskPort
from helpdesk.zendesk_adapter import ZendeskAdapter


def get_helpdesk_port() -> HelpdeskPort:
    """Builds the real ``ZendeskAdapter`` from env vars (see
    ``.env.example``) — identical to what ``ZendeskAdapter()`` already
    defaults to when constructed with no arguments (T-2). No production
    wiring elsewhere in this repo constructs a HelpdeskPort for the app
    process yet (``ingress`` deliberately never invokes the agent graph —
    see its module docstring; that's T-10's scenario runner's job) so this
    is the first place a live app process needs one: the portal's approve
    action (DESIGN §Portal API: "sends via HelpdeskPort").

    Every portal test overrides this dependency
    (``app.dependency_overrides[get_helpdesk_port] = lambda: fake_port``)
    with an in-memory recorder — this function itself is never exercised
    by the test suite, matching DESIGN's boundary: T-8 depends on the
    ``HelpdeskPort`` Protocol, never a concrete adapter.
    """
    return ZendeskAdapter()
