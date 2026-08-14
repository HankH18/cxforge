"""Zendesk OAuth 2.0 authorization-code helper.

Operational helper for docs/zendesk-runbook.md step 2. Not part of the
ticket graph — it exists because the manual curl exchange has several
silent failure modes that all surface as one opaque error.

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

The token value is written to .env and never printed.
"""

from __future__ import annotations

import http.server
import json
import re
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


def authorize_url(subdomain: str, client_id: str, redirect: str) -> str:
    # quote_via=quote so the space in "read write" encodes as %20, matching
    # Zendesk's own documented example, rather than urlencode's default '+'.
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "redirect_uri": redirect,
            "client_id": client_id,
            "scope": SCOPE,
        },
        quote_via=urllib.parse.quote,
    )
    return f"https://{subdomain}.zendesk.com/oauth/authorizations/new?{query}"


def exchange(subdomain: str, client_id: str, client_secret: str, code: str, redirect: str) -> str:
    body = json.dumps(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect,
            "scope": SCOPE,
        }
    ).encode()
    request = urllib.request.Request(
        f"https://{subdomain}.zendesk.com/oauth/tokens",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            token = json.load(response).get("access_token")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:500]
        print(f"HTTP {exc.code}: {detail}\n", file=sys.stderr)
        if "invalid_client" in detail:
            sys.exit(
                "The client_id or client_secret is wrong. client_id is the\n"
                "OAuth client's UNIQUE IDENTIFIER field, not its numeric id."
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
    if not token:
        sys.exit("error: response contained no access_token")
    return str(token)


def write_token(token: str) -> None:
    text = ENV_PATH.read_text()
    if re.search(r"^ZENDESK_OAUTH_TOKEN=", text, flags=re.M):
        text = re.sub(r"^ZENDESK_OAUTH_TOKEN=.*$", f"ZENDESK_OAUTH_TOKEN={token}", text, flags=re.M)
    else:
        text += f"\nZENDESK_OAUTH_TOKEN={token}\n"
    ENV_PATH.write_text(text)


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
        print(f"This exact string must be in the OAuth client's Redirect URLs:\n  {redirect}\n")
        print("Then open this, click Allow, and copy the `code=` value from the")
        print("address bar of the 'cannot connect' page you land on:\n")
        print(authorize_url(subdomain, client_id, redirect))
        print(f"\nThen:  uv run python scripts/{Path(__file__).name} <code>")
        return

    subdomain, client_id, client_secret = require(
        env, "ZENDESK_SUBDOMAIN", "ZENDESK_OAUTH_CLIENT_ID", "ZENDESK_OAUTH_CLIENT_SECRET"
    )
    if mode in {"--serve", "-s"}:
        port = callback_port(redirect)
        if port is None:
            sys.exit(
                f"error: {redirect} is not a localhost URL, so no local listener can\n"
                "receive it. Use the manual flow: --url, then pass the code."
            )
        code = serve_and_capture(authorize_url(subdomain, client_id, redirect), port, redirect)
        print("Code captured. Exchanging ...")
    else:
        code = mode.strip()

    token = exchange(subdomain, client_id, client_secret, code, redirect)
    write_token(token)
    print(f"Success. ZENDESK_OAUTH_TOKEN written to .env ({len(token)} chars, not printed).")
    print("Next: docs/zendesk-runbook.md step 3 for ZENDESK_AI_USER_ID.")


if __name__ == "__main__":
    main(sys.argv[1:])
