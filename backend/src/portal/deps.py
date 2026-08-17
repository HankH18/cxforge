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
    defaults to when constructed with no arguments (T-2). This is the
    *portal's* production HelpdeskPort, used by its approve action (DESIGN
    §Portal API: "sends via HelpdeskPort").

    Until 2026-08-16 this docstring claimed it was the only production
    wiring that constructs a HelpdeskPort, because "``ingress`` deliberately
    never invokes the agent graph … that's T-10's scenario runner's job".
    That was wrong twice over: the scenario runner did not exist, and the
    circular hand-off between this file, ``ingress/__init__.py`` and T-5's
    scope meant **nothing at all** in ``backend/src`` ever called
    ``run_agent`` (``docs/STATE.md §2``). Since W1-A, ``worker.main``
    constructs its own ``ZendeskAdapter()`` for the agent run (ADR-002); the
    two are independent on purpose — the worker's port belongs to a run, and
    this one belongs to an HTTP request.

    Every portal test overrides this dependency
    (``app.dependency_overrides[get_helpdesk_port] = lambda: fake_port``)
    with an in-memory recorder — this function itself is never exercised
    by the test suite, matching DESIGN's boundary: T-8 depends on the
    ``HelpdeskPort`` Protocol, never a concrete adapter.
    """
    return ZendeskAdapter()
