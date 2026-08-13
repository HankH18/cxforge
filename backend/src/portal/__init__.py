"""Portal API: feed, draft edit/approve/reject, gate toggle, metrics.

T-8 implements DESIGN §Portal API / §Metric definitions on this router,
split across ``auth.py`` (X-Portal-Token), ``deps.py`` (HelpdeskPort
injection), ``schemas.py`` (request/response models), ``service.py``
(queries + R13's metric math), and ``routes.py`` (the endpoints
themselves). This module owns only the shared ``router`` instance —
``main.py`` mounts exactly this object — and imports ``routes`` for its
side effect of registering every endpoint onto it.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["portal"])

from portal import routes  # noqa: E402,F401 -- registers endpoints on `router` above
