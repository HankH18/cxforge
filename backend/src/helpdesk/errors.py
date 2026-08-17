"""Typed errors a HelpdeskPort implementation can raise.

Named adapter-agnostically on purpose: a caller catching ``HelpdeskAPIError``
should never need to know which provider raised it. Only ``ZendeskAdapter``
exists yet, but T-3's ``EmailAdapter`` is expected to reuse these rather than
invent its own error hierarchy.
"""

from __future__ import annotations


class HelpdeskError(Exception):
    """Base for every error a HelpdeskPort implementation can raise."""


class HelpdeskConfigError(HelpdeskError):
    """Required configuration (credentials, endpoint) is missing or invalid.

    Distinct from ``HelpdeskAPIError``: this is raised before any network
    call is attempted, so callers can tell "we never reached the provider"
    apart from "the provider rejected the call".
    """


class HelpdeskAPIError(HelpdeskError):
    """A non-retryable failure response from the helpdesk provider.

    Raised immediately for a status code retrying can never fix (a 4xx other
    than 429), and also raised if retries of a transient failure (429, 5xx)
    are exhausted — callers only ever need to handle this one type to know a
    write or read definitively failed.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"helpdesk API error {status_code}: {message}")
        self.status_code = status_code


class HelpdeskAuthError(HelpdeskAPIError):
    """The credential is dead and could not be renewed. Never retried.

    A *subclass* of ``HelpdeskAPIError`` on purpose: every existing caller
    already handles that type to mean "this read or write definitively
    failed", and a credential failure is a strict special case of it. So
    adding this type cannot change the control flow of code that predates
    it (``worker.main.run_ticket``'s ``except Exception``,
    ``portal``'s handlers) — it only lets a caller that *wants* to tell
    "expired credential" apart from "Zendesk rejected the payload" do so.

    Distinct from ``HelpdeskConfigError``, which means a credential was
    never supplied. This one means a credential *was* supplied and is no
    longer usable — the failure mode that read as "someone forgot to
    re-authorize" three separate times because nothing named it.
    """


class RetryableResponse(HelpdeskError):
    """Internal signal for the adapter's retry loop only.

    Never escapes a HelpdeskPort method — the retry loop either recovers
    from it or converts it to ``HelpdeskAPIError`` once retries are
    exhausted.
    """


class RateLimited(RetryableResponse):
    """HTTP 429. Carries the provider's ``Retry-After`` value in seconds."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class ServerUnavailable(RetryableResponse):
    """HTTP 5xx — treated as transient and retried with backoff."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"server error {status_code}")
        self.status_code = status_code
