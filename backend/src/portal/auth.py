"""X-Portal-Token shared-secret auth.

DESIGN §Portal API, pinned verbatim: "Auth: X-Portal-Token shared secret
(env PORTAL_TOKEN). Every endpoint requires it; a missing or wrong token is
a 401. No other auth, no multi-user." This is the ONLY auth check in the
package — every route in ``routes.py`` takes
``Depends(require_portal_token)``.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException

_UNAUTHORIZED_DETAIL = "missing or invalid X-Portal-Token"


def require_portal_token(x_portal_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency every portal endpoint takes.

    Reads ``PORTAL_TOKEN`` from the environment at REQUEST time, not import
    time — mirrors ``ingress``'s ``ZENDESK_WEBHOOK_SIGNING_SECRET`` handling
    (``backend/src/ingress/__init__.py``), so tests can ``monkeypatch.setenv``
    a fixed token per test rather than depending on whatever (if anything)
    the host's real ``.env`` has. An unconfigured server (``PORTAL_TOKEN``
    unset) can never authenticate any request — that is still "a missing ...
    token is a 401" from the caller's point of view, not a separate
    server-error case DESIGN doesn't mention.

    ``secrets.compare_digest`` avoids a timing side-channel on the
    comparison; unlike ``ingress``'s HMAC verification this isn't a
    cryptographic signature, but the same "don't use ``==`` on secrets"
    discipline costs nothing here.
    """
    expected = os.environ.get("PORTAL_TOKEN")
    if not expected or not x_portal_token or not secrets.compare_digest(x_portal_token, expected):
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED_DETAIL)
