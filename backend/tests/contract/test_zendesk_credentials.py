"""The 30-minute access token, and the renewal that makes it survivable.

Every test here is written against a behaviour that was measured on the live
Zendesk account on 2026-08-17, not against an inferred API shape:

* ``ZENDESK_OAUTH_TOKEN`` is a JWT whose payload is exactly ``{"exp": ...}``,
  1800 seconds after issue.
* ``POST /oauth/tokens`` with ``grant_type=refresh_token`` is a supported and
  authorized grant for this client (it answers ``invalid_grant`` for a bad
  refresh-token value, where ``client_credentials`` answers
  ``unauthorized_client`` and an invented grant answers
  ``unsupported_grant_type`` — three distinct errors, so the server does
  separate those cases).
* A ``refresh_token`` is returned alongside every access token with plain
  ``scope="read write"`` and lives 30 days.

The load-bearing test is the FIRST one. The failure this whole module exists
to prevent was not "the token expired" — it was that an expired token still
produced a request, took a 401, and got read as a Zendesk fault three
separate times. So the assertion is not about an exception type, it is that
**no HTTP request is made at all**.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from helpdesk.errors import HelpdeskAPIError, HelpdeskAuthError, HelpdeskConfigError
from helpdesk.zendesk_adapter import ZendeskAdapter
from helpdesk.zendesk_credentials import ZendeskCredentials, access_token_expiry

pytestmark = pytest.mark.contract

NOW = 1_786_946_593.0  # the exp of the real token that expired mid-build
SUBDOMAIN = "test-subdomain"
API_BASE = f"https://{SUBDOMAIN}.zendesk.com/api/v2"


def _b64(payload: dict[str, object]) -> str:
    raw = json.dumps(payload).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def jwt_expiring_at(exp: float) -> str:
    """A token shaped like Zendesk's: one ``exp`` claim, unverifiable signature."""
    return f"{_b64({'typ': 'at+jwt', 'alg': 'EdDSA'})}.{_b64({'exp': int(exp)})}.sig"


class RecordingTransport(httpx.MockTransport):
    """A transport that remembers every request and the bearer it carried."""

    def __init__(self, responder) -> None:  # type: ignore[no-untyped-def]
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return responder(request)

        super().__init__(handler)

    @property
    def bearers(self) -> list[str]:
        return [r.headers.get("authorization", "") for r in self.requests]


def api_client(responder) -> tuple[httpx.Client, RecordingTransport]:  # type: ignore[no-untyped-def]
    transport = RecordingTransport(responder)
    return httpx.Client(base_url=API_BASE, transport=transport), transport


def token_client(responder) -> tuple[httpx.Client, RecordingTransport]:  # type: ignore[no-untyped-def]
    transport = RecordingTransport(responder)
    return (
        httpx.Client(base_url=f"https://{SUBDOMAIN}.zendesk.com", transport=transport),
        transport,
    )


TICKET_OK = {
    "ticket": {
        "id": 4242,
        "subject": "Where is my case?",
        "status": "open",
        "tags": [],
        "created_at": "2026-08-17T05:00:00Z",
        "requester_id": 7,
    },
    "users": [{"id": 7, "email": "someone@example.com", "role": "end-user"}],
}


# --------------------------------------------------------------------------
# Reading the expiry
# --------------------------------------------------------------------------


def test_the_exp_claim_of_a_real_shaped_token_is_read() -> None:
    assert access_token_expiry(jwt_expiring_at(NOW)) == NOW


@pytest.mark.parametrize(
    "token",
    [
        "test-oauth-bearer-token",  # the contract suite's own fixture value
        "not.a.jwt",
        "",
        "two.parts",
        f"{_b64({'alg': 'x'})}.{_b64({'no_exp': 1})}.sig",
    ],
)
def test_a_token_that_states_no_expiry_reports_none_rather_than_expired(token: str) -> None:
    """``None`` must mean "cannot tell", never "treat as dead".

    Opaque pre-2026 Zendesk tokens and every existing test fixture take this
    path; refusing to use them would break callers holding a perfectly good
    credential.
    """
    assert access_token_expiry(token) is None


# --------------------------------------------------------------------------
# The load-bearing one: a dead credential must not produce a request
# --------------------------------------------------------------------------


def test_an_expired_token_that_cannot_be_refreshed_makes_no_http_call_at_all() -> None:
    """THE test. Remove the staleness check and this goes red.

    The historical failure sent the request anyway. `worker.main.run_ticket`
    then caught the 401, released the dedup row and returned — arq booked a
    success, `docker compose ps` stayed healthy, and the only evidence was a
    log line nobody was reading. So what is asserted here is the absence of
    the request, not the presence of an exception.
    """
    client, transport = api_client(lambda request: httpx.Response(200, json=TICKET_OK))
    credentials = ZendeskCredentials(
        subdomain=SUBDOMAIN,
        access_token=jwt_expiring_at(NOW - 60),  # expired a minute ago
        refresh_token=None,  # nothing to renew with
        clock=lambda: NOW,
    )
    adapter = ZendeskAdapter(subdomain=SUBDOMAIN, credentials=credentials, client=client)

    with pytest.raises(HelpdeskAuthError) as exc_info:
        adapter.fetch_ticket("4242")

    assert transport.requests == [], (
        "an expired, unrenewable credential produced a real HTTP request — this "
        "is exactly the invisible 401 loop the credential work exists to remove"
    )
    assert exc_info.value.status_code == 401
    # The message has to name the missing piece, or this becomes the fourth
    # "someone forgot to re-authorize" misdiagnosis.
    assert "ZENDESK_OAUTH_REFRESH_TOKEN" in str(exc_info.value)
    assert "expired" in str(exc_info.value)


def test_a_token_inside_the_leeway_window_is_treated_as_stale() -> None:
    """Not-yet-expired but about to be, with no way to renew, still refuses.

    A run makes many calls over minutes; a token with seconds left will die
    mid-run.
    """
    credentials = ZendeskCredentials(
        subdomain=SUBDOMAIN,
        access_token=jwt_expiring_at(NOW + 30),
        clock=lambda: NOW,
        leeway_seconds=120.0,
    )
    with pytest.raises(HelpdeskAuthError):
        credentials.bearer()


def test_a_healthy_token_is_returned_untouched_and_no_refresh_is_attempted() -> None:
    client, refreshes = token_client(lambda request: httpx.Response(500))
    token = jwt_expiring_at(NOW + 1800)
    credentials = ZendeskCredentials(
        subdomain=SUBDOMAIN,
        access_token=token,
        refresh_token="rt",
        client_id="cid",
        client_secret="secret",
        token_client=client,
        clock=lambda: NOW,
    )
    assert credentials.bearer() == token
    assert refreshes.requests == []


# --------------------------------------------------------------------------
# Renewal, proactive and reactive
# --------------------------------------------------------------------------


def test_an_expired_token_is_refreshed_before_the_request_and_the_new_one_is_sent() -> None:
    """Proactive path: the 401 never happens because the token is replaced first."""
    fresh = jwt_expiring_at(NOW + 1800)
    tokens, refreshes = token_client(
        lambda request: httpx.Response(
            200,
            json={
                "access_token": fresh,
                "refresh_token": "rotated-refresh-token",
                "expires_in": 1800,
                "refresh_token_expires_in": 2592000,
            },
        )
    )
    client, api = api_client(lambda request: httpx.Response(200, json=TICKET_OK))
    credentials = ZendeskCredentials(
        subdomain=SUBDOMAIN,
        access_token=jwt_expiring_at(NOW - 1),
        refresh_token="original-refresh-token",
        client_id="cid",
        client_secret="secret",
        token_client=tokens,
        clock=lambda: NOW,
    )
    adapter = ZendeskAdapter(subdomain=SUBDOMAIN, credentials=credentials, client=client)

    ticket = adapter.fetch_ticket("4242")

    assert ticket.id == "4242"
    # One refresh, sent as the grant Zendesk actually accepts.
    assert len(refreshes.requests) == 1
    grant = json.loads(refreshes.requests[0].content)
    assert grant["grant_type"] == "refresh_token"
    assert grant["refresh_token"] == "original-refresh-token"
    assert grant["client_id"] == "cid"
    assert refreshes.requests[0].url.path == "/oauth/tokens"
    # And the API call carried the NEW token, not the expired one.
    assert api.bearers == [f"Bearer {fresh}"]


def test_the_rotated_refresh_token_replaces_the_old_one() -> None:
    """Zendesk invalidates a refresh token when it is spent.

    Keeping the original would make the *second* refresh fail — a bug that
    would only show up 30 minutes into a demo.
    """
    spent: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        spent.append(body["refresh_token"])
        return httpx.Response(
            200,
            json={
                "access_token": jwt_expiring_at(NOW - 1),  # immediately stale again
                "refresh_token": f"rotated-{len(spent)}",
            },
        )

    tokens, _ = token_client(responder)
    credentials = ZendeskCredentials(
        subdomain=SUBDOMAIN,
        access_token=jwt_expiring_at(NOW - 1),
        refresh_token="original",
        client_id="cid",
        client_secret="secret",
        token_client=tokens,
        clock=lambda: NOW,
    )

    credentials.refresh()
    credentials.refresh()

    assert spent == ["original", "rotated-1"], (
        "the second refresh re-sent a spent refresh token; rotation was not stored"
    )


def test_a_401_mid_flight_triggers_exactly_one_refresh_and_one_retry() -> None:
    """Reactive path: the token looked fine locally but Zendesk rejected it.

    Covers revocation, and the clock skew case where our `exp` arithmetic
    disagrees with Zendesk's.
    """
    # Distinct exp values so the two tokens are distinguishable strings: with
    # the same exp they encode identically and the responder cannot tell the
    # retry from the first attempt.
    fresh = jwt_expiring_at(NOW + 3600)
    tokens, refreshes = token_client(
        lambda request: httpx.Response(200, json={"access_token": fresh})
    )

    def responder(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == f"Bearer {fresh}":
            return httpx.Response(200, json=TICKET_OK)
        return httpx.Response(401, json={"error": "invalid_token"})

    client, api = api_client(responder)
    stale = jwt_expiring_at(NOW + 1800)  # looks healthy; Zendesk disagrees
    credentials = ZendeskCredentials(
        subdomain=SUBDOMAIN,
        access_token=stale,
        refresh_token="rt",
        client_id="cid",
        client_secret="secret",
        token_client=tokens,
        clock=lambda: NOW,
    )
    adapter = ZendeskAdapter(subdomain=SUBDOMAIN, credentials=credentials, client=client)

    assert adapter.fetch_ticket("4242").id == "4242"
    assert len(refreshes.requests) == 1
    assert api.bearers == [f"Bearer {stale}", f"Bearer {fresh}"]


def test_a_permanently_dead_credential_is_not_retried_into_the_ground() -> None:
    """A 401 that a refresh cannot fix must surface, fast and once.

    This is the constraint from ADR-003's reasoning: a retry that masks a
    permanently dead credential is worse than the original bug, because
    `worker/main.py` books the swallowed failure as an arq success.
    """
    tokens, refreshes = token_client(
        lambda request: httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "expired"}
        )
    )
    client, api = api_client(lambda request: httpx.Response(401, json={"error": "invalid_token"}))
    sleeps: list[float] = []
    credentials = ZendeskCredentials(
        subdomain=SUBDOMAIN,
        access_token=jwt_expiring_at(NOW + 1800),
        refresh_token="stale-rt",
        client_id="cid",
        client_secret="secret",
        token_client=tokens,
        clock=lambda: NOW,
    )
    adapter = ZendeskAdapter(
        subdomain=SUBDOMAIN, credentials=credentials, client=client, sleep=sleeps.append
    )

    with pytest.raises(HelpdeskAuthError) as exc_info:
        adapter.fetch_ticket("4242")

    assert len(api.requests) == 1, "the 401 was retried with the same dead token"
    assert len(refreshes.requests) == 1, "the refresh itself was retried"
    assert sleeps == [], "a dead credential must not spend the transient-failure backoff"
    assert exc_info.value.status_code == 400
    assert "invalid_grant" in str(exc_info.value)


def test_a_401_that_survives_a_successful_refresh_raises_rather_than_looping() -> None:
    """Refresh worked, Zendesk still says no (scopes, deleted user, ...)."""
    tokens, refreshes = token_client(
        lambda request: httpx.Response(200, json={"access_token": jwt_expiring_at(NOW + 1800)})
    )
    client, api = api_client(lambda request: httpx.Response(401, json={"error": "invalid_token"}))
    credentials = ZendeskCredentials(
        subdomain=SUBDOMAIN,
        access_token=jwt_expiring_at(NOW + 1800),
        refresh_token="rt",
        client_id="cid",
        client_secret="secret",
        token_client=tokens,
        clock=lambda: NOW,
    )
    adapter = ZendeskAdapter(subdomain=SUBDOMAIN, credentials=credentials, client=client)

    with pytest.raises(HelpdeskAuthError):
        adapter.fetch_ticket("4242")

    assert len(refreshes.requests) == 1, "more than one refresh per request"
    assert len(api.requests) == 2, "expected exactly one retry after the refresh"


def test_a_refresh_failure_is_logged_at_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ADR-003 makes the ERROR log load-bearing: arq's own accounting lies."""
    tokens, _ = token_client(lambda request: httpx.Response(400, json={"error": "invalid_grant"}))
    credentials = ZendeskCredentials(
        subdomain=SUBDOMAIN,
        access_token=jwt_expiring_at(NOW - 1),
        refresh_token="rt",
        client_id="cid",
        client_secret="secret",
        token_client=tokens,
        clock=lambda: NOW,
    )
    with caplog.at_level("ERROR"), pytest.raises(HelpdeskAuthError):
        credentials.bearer()

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "a dead credential failed silently"
    assert "zendesk_oauth.py" in errors[0].getMessage(), (
        "the ERROR must say how to fix it, or it becomes another misdiagnosis"
    )


# --------------------------------------------------------------------------
# What must NOT change
# --------------------------------------------------------------------------


def test_a_non_401_error_never_touches_the_credential() -> None:
    """A 422 is a payload problem. Refreshing would hide it."""
    tokens, refreshes = token_client(lambda request: httpx.Response(200, json={}))
    client, api = api_client(lambda request: httpx.Response(422, json={"error": "RecordInvalid"}))
    credentials = ZendeskCredentials(
        subdomain=SUBDOMAIN,
        access_token=jwt_expiring_at(NOW + 1800),
        refresh_token="rt",
        client_id="cid",
        client_secret="secret",
        token_client=tokens,
        clock=lambda: NOW,
    )
    adapter = ZendeskAdapter(subdomain=SUBDOMAIN, credentials=credentials, client=client)

    with pytest.raises(HelpdeskAPIError) as exc_info:
        adapter.fetch_ticket("4242")

    assert exc_info.value.status_code == 422
    assert not isinstance(exc_info.value, HelpdeskAuthError)
    assert refreshes.requests == []
    assert len(api.requests) == 1


def test_no_credential_at_all_is_still_a_config_error() -> None:
    """Distinct from HelpdeskAuthError: nothing was ever supplied."""
    with pytest.raises(HelpdeskConfigError):
        ZendeskCredentials(subdomain=SUBDOMAIN, access_token=None, refresh_token=None)
