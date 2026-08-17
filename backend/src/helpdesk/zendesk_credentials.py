"""The Zendesk access token's lifetime, and the only thing that renews it.

WHY THIS MODULE EXISTS — measured, not inferred, on 2026-08-17 against the
live account:

``ZENDESK_OAUTH_TOKEN`` is not an opaque long-lived Zendesk token. It is a
JWT (``typ: at+jwt``, ``alg: EdDSA``) whose payload carries exactly one
claim::

    {"exp": 1786946593}     ->  2026-08-17T06:03:13Z

``GET /api/v2/oauth/tokens/current.json`` reported ``created_at``
06:35:52 / ``expires_at`` 07:05:52 for the following token: a **1800-second**
life, which is Zendesk's documented 30-minute default for OAuth clients
created after 2026-04-30. ``expires_in`` is not a lever — minting with
86400, 172800 and 604800 all produced a 1800s token.

The failure that follows is invisible, which is the actual problem this
module solves. ``agent.nodes.ingest``'s first statement is
``deps.port.fetch_ticket(ticket_id)``; with an expired token that 401s,
``worker.main.run_ticket`` catches it, releases the dedup row (ADR-003) and
returns. Every run fails, no ``runs`` row is ever written, and
``docker compose ps`` plus ``verify_deploy.sh`` both stay green. It was
misdiagnosed as "someone forgot to re-authorize" three times.

WHAT MAKES IT RENEWABLE. Zendesk returns a ``refresh_token`` alongside every
access token — ``refresh_token_expires_at`` was 30 days out — and it does so
with plain ``scope="read write"``: **no ``offline_access`` scope is
involved**, contrary to the standing theory that one was needed. The reason
no refresh token was ever available is that ``scripts/zendesk_oauth.py``
parsed ``access_token`` out of the grant response and dropped the rest.
That endpoint is also the only place the refresh token is readable in full
(``oauth/tokens/current.json`` masks it as ``"...DM7OM4PKDA"``), so a
dropped value is unrecoverable without another browser consent.

That the client is authorized for the grant is a three-way control
experiment, not a guess — ``POST /oauth/tokens`` on this account answers:

===========================  ==========================  ====================
``grant_type``               error                       means
===========================  ==========================  ====================
``refresh_token``            ``invalid_grant``           supported + authorized,
                                                         the *value* was bad
``client_credentials``       ``unauthorized_client``      supported, client not
                                                         authorized for it
``banana_grant``             ``unsupported_grant_type``   server does not know it
===========================  ==========================  ====================

Three distinct errors, so the server does separate those cases;
``refresh_token`` lands in the one that means "we accept this grant from
this client".

THE ONE RULE HERE: never hand out a credential known to be dead. ``bearer()``
reads the ``exp`` claim locally — no network — and refuses rather than
letting a doomed request go out and 401. A refresh that fails raises
``HelpdeskAuthError`` and logs ERROR, because ERROR is the only honest signal
in this stack (``worker/main.py``: arq books a swallowed failure as
``success = True``).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time
from collections.abc import Callable

import httpx

from helpdesk.errors import HelpdeskAuthError, HelpdeskConfigError

logger = logging.getLogger(__name__)

# Refresh this many seconds before the stated `exp`. A token that expires
# mid-flight is indistinguishable from a revoked one at the 401, and a run
# makes many calls over minutes, so the margin is generous rather than tight.
DEFAULT_LEEWAY_SECONDS = 120.0

# Zendesk's own default, recorded so a surprise is legible in a log line.
OBSERVED_ACCESS_TOKEN_LIFETIME_SECONDS = 1800


def access_token_expiry(token: str) -> float | None:
    """The ``exp`` claim of a Zendesk access token, or ``None``.

    ``None`` means "this token does not tell us when it expires" — an opaque
    (pre-2026) Zendesk token, or a test fixture like
    ``"test-oauth-bearer-token"``. That is deliberately NOT treated as
    expired: refusing to use a credential we cannot read would break every
    caller holding a legitimately opaque token. It only means the proactive
    check cannot help, and the reactive 401 path is what covers it.

    The signature is **not** verified, and must not be: we are not
    authenticating the token, we are reading the expiry its issuer stated so
    we can avoid a request we know will fail. Zendesk is the only party that
    validates it.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    if isinstance(exp, int | float) and not isinstance(exp, bool):
        return float(exp)
    return None


class ZendeskCredentials:
    """Holds the Zendesk access token and renews it from the refresh token.

    One instance per adapter. ``bearer()`` is called on every request, so the
    access token in use is never older than the last refresh.
    """

    def __init__(
        self,
        *,
        subdomain: str,
        access_token: str | None = None,
        refresh_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
        leeway_seconds: float = DEFAULT_LEEWAY_SECONDS,
        on_refresh: Callable[[str, str | None], None] | None = None,
    ) -> None:
        self._subdomain = subdomain
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._clock = clock
        self._leeway = leeway_seconds
        self._on_refresh = on_refresh
        # The token endpoint lives at the account root, NOT under /api/v2 —
        # a client built for the API base URL cannot reach it.
        self._token_client = token_client or httpx.Client(
            base_url=f"https://{subdomain}.zendesk.com", timeout=10.0
        )
        if not self._access_token and not self._refresh_token:
            raise HelpdeskConfigError(
                "ZendeskCredentials needs at least one of ZENDESK_OAUTH_TOKEN "
                "or ZENDESK_OAUTH_REFRESH_TOKEN."
            )

    @classmethod
    def from_env(cls, *, subdomain: str, access_token: str | None = None) -> ZendeskCredentials:
        """Build from the environment.

        Every key below is a **literal** string so
        ``backend/tests/deploy/test_env_forwarding.py``'s AST pass (rule 1)
        resolves it and requires it in every application container. A read
        through a module constant is unresolvable, lands in that module's
        ``KNOWN_DYNAMIC_ENV_READS`` ledger, and is then required by nothing —
        so do not DRY these literals away.
        """
        return cls(
            subdomain=subdomain,
            access_token=access_token or os.environ.get("ZENDESK_OAUTH_TOKEN"),
            refresh_token=os.environ.get("ZENDESK_OAUTH_REFRESH_TOKEN"),
            client_id=os.environ.get("ZENDESK_OAUTH_CLIENT_ID"),
            client_secret=os.environ.get("ZENDESK_OAUTH_CLIENT_SECRET"),
        )

    # -- state ------------------------------------------------------------

    @property
    def can_refresh(self) -> bool:
        return bool(self._refresh_token and self._client_id and self._client_secret)

    def expires_at(self) -> float | None:
        """When the held access token expires, if it says."""
        if not self._access_token:
            return None
        return access_token_expiry(self._access_token)

    def seconds_remaining(self) -> float | None:
        expiry = self.expires_at()
        return None if expiry is None else expiry - self._clock()

    def _is_stale(self) -> bool:
        """True when the token is expired, or close enough that it will be."""
        remaining = self.seconds_remaining()
        return remaining is not None and remaining <= self._leeway

    # -- use --------------------------------------------------------------

    def bearer(self) -> str:
        """A usable access token, refreshed first if the held one is stale.

        Raises rather than returning a credential known to be dead. This is
        the whole point of the module: the pre-existing failure sent the
        request anyway, took a 401, and looked like a Zendesk problem.
        """
        if self._access_token and not self._is_stale():
            return self._access_token

        if not self._access_token:
            reason = "no ZENDESK_OAUTH_TOKEN is set"
        else:
            remaining = self.seconds_remaining()
            assert remaining is not None  # _is_stale() is False when it is None
            reason = (
                f"the access token expired {abs(int(remaining))}s ago"
                if remaining <= 0
                else f"the access token expires in {int(remaining)}s"
            )

        if not self.can_refresh:
            logger.error(
                "Zendesk credential is unusable and cannot be renewed: %s, and "
                "%s. Re-authorize with `uv run python scripts/zendesk_oauth.py "
                "--serve` (docs/OWNER-ACTIONS.md OA-4).",
                reason,
                self._why_not_refreshable(),
            )
            raise HelpdeskAuthError(
                401,
                f"Zendesk credential unusable and not renewable: {reason}, and "
                f"{self._why_not_refreshable()}. No request was attempted.",
            )

        logger.info("refreshing the Zendesk access token proactively: %s", reason)
        return self.refresh()

    def _why_not_refreshable(self) -> str:
        missing = [
            name
            for name, value in (
                ("ZENDESK_OAUTH_REFRESH_TOKEN", self._refresh_token),
                ("ZENDESK_OAUTH_CLIENT_ID", self._client_id),
                ("ZENDESK_OAUTH_CLIENT_SECRET", self._client_secret),
            )
            if not value
        ]
        return f"no refresh is possible without {', '.join(missing)}"

    def refresh(self) -> str:
        """Spend the refresh token for a new access token. Loud on failure.

        Returns the new access token. Raises ``HelpdeskAuthError`` — after an
        ERROR log — for anything else, and never returns the stale token as a
        fallback: masking a permanently dead credential behind a retry is the
        failure mode this whole change exists to remove.
        """
        if not self.can_refresh:
            logger.error("cannot refresh the Zendesk token: %s", self._why_not_refreshable())
            raise HelpdeskAuthError(401, f"cannot refresh: {self._why_not_refreshable()}")

        try:
            response = self._token_client.post(
                "/oauth/tokens",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        except httpx.HTTPError as exc:
            logger.error("the Zendesk token refresh request itself failed: %r", exc)
            raise HelpdeskAuthError(0, f"token refresh request failed: {exc}") from None

        if response.status_code != 200:
            # A refresh token is single-use (Zendesk rotates it) and lives 30
            # days, so this is usually "already spent" or "expired" — both
            # need a human at a browser, which is why it is ERROR and not a
            # retry.
            logger.error(
                "Zendesk refused the token refresh (HTTP %s): %s. The refresh "
                "token is spent, expired or revoked — re-authorize with "
                "`uv run python scripts/zendesk_oauth.py --serve` "
                "(docs/OWNER-ACTIONS.md OA-4).",
                response.status_code,
                response.text[:300],
            )
            raise HelpdeskAuthError(
                response.status_code,
                f"token refresh rejected: {response.text[:300]}",
            )

        try:
            payload = response.json()
        except ValueError as exc:
            logger.error("the Zendesk token refresh returned non-JSON: %r", exc)
            raise HelpdeskAuthError(0, f"token refresh returned non-JSON: {exc}") from None

        access_token = payload.get("access_token")
        if not access_token or not isinstance(access_token, str):
            logger.error("the Zendesk token refresh response carried no access_token")
            raise HelpdeskAuthError(0, "token refresh response carried no access_token")

        self._access_token = access_token
        rotated = payload.get("refresh_token")
        if isinstance(rotated, str) and rotated:
            # Zendesk ROTATES: "Always replace both your access token and
            # refresh token with the new values returned by Zendesk as the
            # previous ones are now invalid." Keeping the old one would make
            # the *next* refresh fail.
            self._refresh_token = rotated

        remaining = self.seconds_remaining()
        logger.info(
            "refreshed the Zendesk access token",
            extra={
                "expires_in_s": None if remaining is None else int(remaining),
                "refresh_token_rotated": bool(rotated),
            },
        )
        if self._on_refresh is not None:
            self._on_refresh(access_token, self._refresh_token)
        elif rotated:
            # In a container there is no `.env` to write back to, so the
            # rotated value lives only in this process. That is fine while the
            # worker runs and NOT fine across a restart, so say so once.
            logger.warning(
                "the Zendesk refresh token rotated and was not persisted — this "
                "process will keep working, but a restart falls back to the "
                "stale ZENDESK_OAUTH_REFRESH_TOKEN in the environment and will "
                "need `scripts/zendesk_oauth.py --serve`."
            )
        return access_token
