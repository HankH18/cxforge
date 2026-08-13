"""Typed errors the portal service layer raises.

Named the way ``helpdesk.errors`` is: a caller catching ``DraftNotFound``
should never have to know it came from the ``drafts`` table specifically
rather than some other lookup. ``routes.py`` is the only place these get
translated to HTTP status codes (404 / 409) — the service layer itself
never touches FastAPI.
"""

from __future__ import annotations


class PortalError(Exception):
    """Base for every error the portal service layer raises."""


class DraftNotFound(PortalError):
    def __init__(self, draft_id: int) -> None:
        super().__init__(f"no such draft: {draft_id}")
        self.draft_id = draft_id


class DraftNotPending(PortalError):
    """Raised by edit/approve/reject when the draft has already left the
    ``pending`` state.

    This is the single guard that makes editing an already-sent draft, a
    double-approve, and approving-or-rejecting an already-rejected/approved
    draft all defined errors instead of a silent no-op or (worse) a second
    port send. ``status`` carries the draft's actual current state so the
    HTTP layer's error detail can say what it is, not just that it wasn't
    "pending"."""

    def __init__(self, draft_id: int, status: str | None) -> None:
        super().__init__(f"draft {draft_id} is not pending (status={status!r})")
        self.draft_id = draft_id
        self.status = status
