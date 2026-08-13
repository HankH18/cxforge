"""X-Portal-Token auth — DESIGN §Portal API, pinned verbatim: "Every
endpoint requires it; a missing or wrong token is a 401."

Every request in this file sends a body valid enough to pass Pydantic
validation on endpoints that take one, so a 401 can never be confused with
a 422 — auth is the ONLY thing under test here.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH_HEADERS

WRONG_HEADERS = {"X-Portal-Token": "not-the-real-token"}

# (method, path, json body) for every pinned endpoint.
_ENDPOINTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("get", "/api/feed", None),
    ("put", "/api/drafts/1", {"body": "hello"}),
    ("post", "/api/drafts/1/approve", None),
    ("post", "/api/drafts/1/reject", None),
    ("get", "/api/settings/gate", None),
    ("put", "/api/settings/gate", {"enabled": True}),
    ("get", "/api/metrics", None),
]


@pytest.mark.parametrize("method,path,body", _ENDPOINTS)
def test_missing_token_is_401(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = client.request(method.upper(), path, json=body)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", _ENDPOINTS)
def test_wrong_token_is_401(
    client: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = client.request(method.upper(), path, json=body, headers=WRONG_HEADERS)
    assert response.status_code == 401


def test_correct_token_clears_auth(client: TestClient) -> None:
    """Sanity check on the fixtures above: the right token is never itself
    the blocker (``/api/metrics`` needs no path param, so it can only ever
    401 on auth, never 404 on a missing resource)."""
    response = client.get("/api/metrics", headers=AUTH_HEADERS)
    assert response.status_code == 200
