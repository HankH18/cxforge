"""The portal endpoints themselves — DESIGN §Portal API, pinned verbatim:

    GET  /api/feed?status=
    PUT  /api/drafts/{id}
    POST /api/drafts/{id}/approve
    POST /api/drafts/{id}/reject
    GET|PUT /api/settings/gate
    GET  /api/metrics

Every route takes ``Depends(require_portal_token)`` — DESIGN: "Every
endpoint requires it; a missing or wrong token is a 401." This module is a
thin FastAPI shim: it translates HTTP <-> ``portal.service``'s plain-Python
calls and ``portal.errors``'s typed exceptions <-> status codes, and
contains no business logic of its own.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from helpdesk.errors import HelpdeskError
from helpdesk.port import HelpdeskPort
from portal import router, service
from portal.auth import require_portal_token
from portal.deps import get_helpdesk_port
from portal.errors import DraftNotFound, DraftNotPending
from portal.schemas import (
    DraftEditRequest,
    DraftResponse,
    DraftStatus,
    FeedResponse,
    GateSetting,
    MetricsResponse,
)

_AUTH = [Depends(require_portal_token)]
_PORT = Depends(get_helpdesk_port)


@router.get("/feed", response_model=FeedResponse, dependencies=_AUTH)
def get_feed(status: DraftStatus | None = None) -> FeedResponse:
    return FeedResponse(runs=service.fetch_feed(status))


@router.put("/drafts/{draft_id}", response_model=DraftResponse, dependencies=_AUTH)
def edit_draft(draft_id: int, edit: DraftEditRequest) -> DraftResponse:
    try:
        return service.edit_draft(draft_id, edit.body)
    except DraftNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DraftNotPending as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/drafts/{draft_id}/approve", response_model=DraftResponse, dependencies=_AUTH)
def approve_draft(draft_id: int, port: HelpdeskPort = _PORT) -> DraftResponse:
    try:
        return service.approve_draft(draft_id, port)
    except DraftNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DraftNotPending as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HelpdeskError as exc:
        # Upstream (Zendesk/email) failed the send — the draft is left
        # `pending` (service.approve_draft's transaction rolled back), so
        # this is retryable, not a 500: the portal itself did nothing
        # wrong.
        raise HTTPException(status_code=502, detail=f"helpdesk send failed: {exc}") from exc


@router.post("/drafts/{draft_id}/reject", response_model=DraftResponse, dependencies=_AUTH)
def reject_draft(draft_id: int) -> DraftResponse:
    try:
        return service.reject_draft(draft_id)
    except DraftNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DraftNotPending as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/settings/gate", response_model=GateSetting, dependencies=_AUTH)
def get_gate() -> GateSetting:
    return GateSetting(enabled=service.read_gate())


@router.put("/settings/gate", response_model=GateSetting, dependencies=_AUTH)
def put_gate(setting: GateSetting) -> GateSetting:
    service.write_gate(setting.enabled)
    return GateSetting(enabled=service.read_gate())


@router.get("/metrics", response_model=MetricsResponse, dependencies=_AUTH)
def get_metrics() -> MetricsResponse:
    return service.compute_metrics()
