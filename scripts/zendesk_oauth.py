"""Zendesk OAuth 2.0 authorization-code helper.

Operational helper for docs/zendesk-runbook.md step 2. Not part of the
ticket graph — it exists because the manual curl exchange has several
silent failure modes that all surface as one opaque error.

ACCESS TOKENS EXPIRE, AND FAST. Measured 2026-08-17 against the live
account: the access token is a JWT (`typ: at+jwt`, `alg: EdDSA`) whose
payload carries exactly one claim, `exp`, set **1800 seconds** after issue.
An earlier revision of this file and of the runbook claimed these tokens are
long-lived until revoked; that is false and it cost three misdiagnoses of
"someone forgot to re-authorize". `expires_in` is NOT a usable lever —
minting with 86400 / 172800 / 604800 all produced a 1800s token.

WHICH IS WHY THIS SCRIPT NOW SAVES THE REFRESH TOKEN. Zendesk returns a
`refresh_token` (30-day life) alongside every access token, with no
`offline_access` scope required — the previous revision parsed only
`access_token` out of the response and silently dropped it, which is the
whole reason the credential looked un-renewable. `--refresh` spends it for a
new access token with no browser involved.

REDIRECT URI: Zendesk requires redirect URLs to be "absolute and not
relative" and "secure (https) unless you're using localhost or 127.0.0.1".
The out-of-band URN (urn:ietf:wg:oauth:2.0:oob) is NOT an http(s) URL and
Zendesk rejects it at the authorize step with `invalid_request`. So this
uses a localhost callback, which is explicitly permitted.

    http://localhost:8129/callback

That exact string must be registered in the OAuth client's "Redirect URLs"
field (Admin Center -> Apps and integrations -> APIs -> Zendesk API ->
OAuth Clients -> your client). Zendesk requires the redirect_uri parameter
to match a registered URL.

Usage, from the repo root:

    # easiest — starts a local listener, prints the URL, captures the code,
    # exchanges it, and writes the token to .env. No copy-paste of codes.
    uv run python scripts/zendesk_oauth.py --serve

    # manual fallback if you cannot run a listener: print the URL, approve
    # in the browser, then copy the `code=` value out of the address bar of
    # the "can't connect" page you land on.
    uv run python scripts/zendesk_oauth.py --url
    uv run python scripts/zendesk_oauth.py <code>

    # no browser at all: spend ZENDESK_OAUTH_REFRESH_TOKEN for a fresh
    # access token. This is the one to run right before a demo take or a
    # Wave 4 scenario run.
    uv run python scripts/zendesk_oauth.py --refresh

Token values are written to .env and never printed.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import re
import secrets
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_REDIRECT_URI = "http://localhost:8129/callback"
SCOPE = "read write"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
CALLBACK_TIMEOUT_S = 300
# Where the PKCE verifier is parked between --url and the code exchange, since
# those are two separate processes. Deleted after use.
VERIFIER_PATH = Path(__file__).resolve().parent.parent / ".zendesk_pkce_verifier"


def make_pkce() -> tuple[str, str]:
    """Return (verifier, challenge) for PKCE S256.

    Zendesk reports `kind: "public"` for this OAuth client. Public clients
    cannot hold a secret, so PKCE is REQUIRED, not optional: without
    code_challenge the authorize endpoint rejects the request as
    `invalid_request - missing a required parameter`, which is exactly the
    error this flow hit.
    """
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def redirect_uri(env: dict[str, str]) -> str:
    """The redirect URI must EXACTLY match one registered on the OAuth client.

    Zendesk validates it at the authorize step and rejects a mismatch with
    `invalid_request` before issuing any code — indistinguishable from a
    malformed request. Rather than force the registered value to match a
    hardcoded string, take it from .env so whatever is already registered
    wins. ZENDESK_OAUTH_REDIRECT_URI overrides; otherwise the default below,
    which is what the runbook tells you to register.
    """
    return env.get("ZENDESK_OAUTH_REDIRECT_URI") or DEFAULT_REDIRECT_URI


def callback_port(uri: str) -> int | None:
    """Port for the local listener, or None if the URI is not a localhost one."""
    parsed = urllib.parse.urlparse(uri)
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        return None
    return parsed.port or (443 if parsed.scheme == "https" else 80)


def load_env() -> dict[str, str]:
    """Read .env from disk — never trust the caller's exported shell state.

    A shell sourced before .env was filled in is one of the failure modes
    this script exists to remove.
    """
    if not ENV_PATH.exists():
        sys.exit(f"error: {ENV_PATH} not found. Copy .env.example to .env first.")
    env: dict[str, str] = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def require(env: dict[str, str], *keys: str) -> list[str]:
    missing = [k for k in keys if not env.get(k)]
    if missing:
        sys.exit(
            "error: these are empty in .env: "
            + ", ".join(missing)
            + "\nSee docs/zendesk-runbook.md step 2."
        )
    return [env[k] for k in keys]


def authorize_url(subdomain: str, client_id: str, redirect: str, challenge: str) -> str:
    # quote_via=quote so the space in "read write" encodes as %20, matching
    # Zendesk's own documented example, rather than urlencode's default '+'.
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "redirect_uri": redirect,
            "client_id": client_id,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        quote_via=urllib.parse.quote,
    )
    return f"https://{subdomain}.zendesk.com/oauth/authorizations/new?{query}"


def _post_grant(
    subdomain: str, payload: dict[str, str], *, redirect: str = ""
) -> dict[str, object]:
    """POST a grant to /oauth/tokens and return the parsed token response.

    Returns the WHOLE response, not just ``access_token``. Zendesk also
    returns ``refresh_token``, ``expires_in`` and ``refresh_token_expires_in``
    here, and this endpoint is the ONLY place the refresh token is ever
    readable in full: ``GET /api/v2/oauth/tokens/current.json`` reports it
    masked (``"...DM7OM4PKDA"``), so a value dropped here is unrecoverable
    without another browser consent. That is exactly what the previous
    revision did.
    """
    request = urllib.request.Request(
        f"https://{subdomain}.zendesk.com/oauth/tokens",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            parsed = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:500]
        print(f"HTTP {exc.code}: {detail}\n", file=sys.stderr)
        if "invalid_client" in detail:
            sys.exit(
                "The client_id or client_secret is wrong. client_id is the\n"
                "OAuth client's UNIQUE IDENTIFIER field, not its numeric id."
            )
        if "invalid_grant" in detail and payload["grant_type"] == "refresh_token":
            sys.exit(
                "The REFRESH TOKEN was rejected — client credentials are fine.\n"
                "  1. Refresh tokens live 30 days, and Zendesk rotates them: the\n"
                "     value is invalidated the first time it is spent, so a stale\n"
                "     copy in .env fails here.\n"
                "  2. Re-run the browser flow to get a new pair:\n"
                "       uv run python scripts/zendesk_oauth.py --serve"
            )
        if "invalid_grant" in detail:
            sys.exit(
                "Client credentials are FINE — the authorization code was rejected:\n"
                "  1. The code was already used or expired (single-use, short-lived).\n"
                f"  2. The OAuth client's 'Redirect URLs' must contain exactly:\n"
                f"       {redirect}\n"
                "     Zendesk reports a redirect mismatch as invalid_grant too.\n"
                "  3. The code was truncated on paste."
            )
        raise
    if not parsed.get("access_token"):
        sys.exit("error: response contained no access_token")
    return dict(parsed)


def exchange(
    subdomain: str, client_id: str, client_secret: str, code: str, redirect: str, verifier: str
) -> dict[str, object]:
    return _post_grant(
        subdomain,
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect,
            "scope": SCOPE,
            "code_verifier": verifier,
        },
        redirect=redirect,
    )


def refresh(
    subdomain: str, client_id: str, client_secret: str, refresh_token: str
) -> dict[str, object]:
    return _post_grant(
        subdomain,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )


def write_env(pairs: dict[str, str]) -> None:
    """Upsert each KEY=value into .env, leaving every other line alone."""
    text = ENV_PATH.read_text()
    for key, value in pairs.items():
        if re.search(rf"^{key}=", text, flags=re.M):
            text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
        else:
            text += f"\n{key}={value}\n"
    ENV_PATH.write_text(text)


def save_tokens(response: dict[str, object]) -> None:
    """Persist the access token AND the rotated refresh token together.

    Both or neither: a saved access token with a stale refresh token beside
    it is the state that makes ``--refresh`` fail 30 minutes later for no
    visible reason.
    """
    pairs = {"ZENDESK_OAUTH_TOKEN": str(response["access_token"])}
    refresh_token = response.get("refresh_token")
    if refresh_token:
        pairs["ZENDESK_OAUTH_REFRESH_TOKEN"] = str(refresh_token)
    write_env(pairs)

    access_life = response.get("expires_in")
    refresh_life = response.get("refresh_token_expires_in")
    print(
        f"Success. ZENDESK_OAUTH_TOKEN written to .env "
        f"({len(str(response['access_token']))} chars, not printed)."
    )
    if access_life:
        print(f"  access token expires in {access_life}s (~{int(access_life) // 60} min)")
    if refresh_token:
        note = "  ZENDESK_OAUTH_REFRESH_TOKEN also written"
        if refresh_life:
            days = int(str(refresh_life)) // 86400
            note += f", expires in {refresh_life}s (~{days} days)"
        print(note)
        print("  Renew later with no browser:  uv run python scripts/zendesk_oauth.py --refresh")
    else:
        print(
            "  WARNING: Zendesk returned NO refresh_token, so --refresh will not\n"
            "  work and every renewal needs a browser consent. Check the OAuth\n"
            "  client's configuration before relying on an unattended run."
        )


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures ?code=... from Zendesk's redirect, then tells the browser."""

    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.code = (params.get("code") or [None])[0]
        _CallbackHandler.error = (params.get("error") or [None])[0]
        ok = _CallbackHandler.code is not None
        message = (
            "Authorization captured. You can close this tab and return to the terminal."
            if ok
            else f"No code in callback. Zendesk said: {_CallbackHandler.error or 'nothing'}"
        )
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(message.encode())

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


def serve_and_capture(url: str, port: int, redirect: str) -> str:
    if _port_in_use(port):
        sys.exit(
            f"error: port {port} is already in use, so the callback\n"
            "listener cannot start. Free it, or use the manual --url flow."
        )
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = CALLBACK_TIMEOUT_S
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("Open this in a browser logged into Zendesk and click Allow:\n")
    print(url)
    print(f"\nWaiting up to {CALLBACK_TIMEOUT_S}s for the redirect to {redirect} ...")
    thread.join(timeout=CALLBACK_TIMEOUT_S)
    server.server_close()

    if _CallbackHandler.error:
        sys.exit(f"Zendesk returned an error instead of a code: {_CallbackHandler.error}")
    if not _CallbackHandler.code:
        sys.exit(
            "timed out with no callback received.\n"
            f"Check that the OAuth client's 'Redirect URLs' contains exactly:\n  {redirect}"
        )
    return _CallbackHandler.code


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def main(argv: list[str]) -> None:
    env = load_env()
    if len(argv) != 1:
        sys.exit(__doc__)
    mode = argv[0]

    redirect = redirect_uri(env)

    if mode in {"--url", "-u"}:
        subdomain, client_id = require(env, "ZENDESK_SUBDOMAIN", "ZENDESK_OAUTH_CLIENT_ID")
        verifier, challenge = make_pkce()
        VERIFIER_PATH.write_text(verifier)
        print(f"This exact string must be in the OAuth client's Redirect URLs:\n  {redirect}\n")
        print("Then open this, click Allow, and copy the `code=` value from the")
        print("address bar of the 'cannot connect' page you land on:\n")
        print(authorize_url(subdomain, client_id, redirect, challenge))
        print(f"\nThen:  uv run python scripts/{Path(__file__).name} <code>")
        return

    subdomain, client_id, client_secret = require(
        env, "ZENDESK_SUBDOMAIN", "ZENDESK_OAUTH_CLIENT_ID", "ZENDESK_OAUTH_CLIENT_SECRET"
    )

    if mode in {"--refresh", "-r"}:
        (refresh_token,) = require(env, "ZENDESK_OAUTH_REFRESH_TOKEN")
        print("Spending the refresh token for a new access token (no browser needed) ...")
        save_tokens(refresh(subdomain, client_id, client_secret, refresh_token))
        return

    verifier, challenge = make_pkce()

    if mode in {"--serve", "-s"}:
        port = callback_port(redirect)
        if port is None:
            sys.exit(
                f"error: {redirect} is not a localhost URL, so no local listener can\n"
                "receive it. Use the manual flow: --url, then pass the code."
            )
        code = serve_and_capture(
            authorize_url(subdomain, client_id, redirect, challenge), port, redirect
        )
        print("Code captured. Exchanging ...")
    else:
        code = mode.strip()
        if not VERIFIER_PATH.exists():
            sys.exit(
                "error: no PKCE verifier on disk. This client is a PUBLIC OAuth\n"
                "client, so the code is bound to the challenge sent at authorize\n"
                "time. Run --url (or --serve) first, then exchange."
            )
        verifier = VERIFIER_PATH.read_text().strip()

    response = exchange(subdomain, client_id, client_secret, code, redirect, verifier)
    VERIFIER_PATH.unlink(missing_ok=True)
    save_tokens(response)
    print("Next: docs/zendesk-runbook.md step 3 for ZENDESK_AI_USER_ID.")


if __name__ == "__main__":
    main(sys.argv[1:])
