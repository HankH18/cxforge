"""Portal API: feed, draft edit/approve/reject, gate toggle, metrics.

Scaffold only — T-8 implements the endpoints on this router.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["portal"])
