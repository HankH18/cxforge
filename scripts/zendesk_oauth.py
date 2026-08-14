"""Zendesk OAuth 2.0 authorization-code helper.

Operational helper for docs/zendesk-runbook.md step 2. Not part of the
ticket graph — it exists because the manual curl exchange has several
silent failure modes (stale shell env, single-use codes, redirect_uri
mismatch) that all surface as the same opaque `invalid_grant`.

Deliberately NON-interactive: it takes the code as an argument rather than
prompting, because `input()` has no TTY when run through Claude Code's `!`
prefix and dies with "unknown terminal".

Usage, from the repo root:

    # 1. print the authorization URL to open in a browser
    uv run python scripts/zendesk_oauth.py --url

    # 2. paste the code from the redirected URL (use it immediately —
    #    Zendesk codes are single-use and short-lived)
    uv run python scripts/zendesk_oauth.py <code>

Step 2 writes ZENDESK_OAUTH_TOKEN into .env on success. The token value is
never printed.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
SCOPE = "read write"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env() -> dict[str, str]:
    """Read .env directly — never rely on the caller's exported shell state.

    A stale shell (sourced before .env was filled in) is one of the failure
    modes this script exists to remove.
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


def authorize_url(subdomain: str, client_id: str) -> str:
    # quote_via=quote so the space in "read write" encodes as %20 rather than
    # urlencode's default '+'. The runbook and Zendesk's own examples use %20;
    # not worth discovering the hard way whether their parser treats them alike.
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "scope": SCOPE,
        },
        quote_via=urllib.parse.quote,
    )
    return f"https://{subdomain}.zendesk.com/oauth/authorizations/new?{query}"


def exchange(subdomain: str, client_id: str, client_secret: str, code: str) -> str:
    body = json.dumps(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
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
                "The client_id or client_secret is wrong.\n"
                "  Admin Center -> Apps and integrations -> APIs -> Zendesk API\n"
                "  -> OAuth Clients -> your client. client_id is the UNIQUE\n"
                "  IDENTIFIER field. The secret is shown only at creation; if\n"
                "  it was lost, delete the client and recreate it."
            )
        if "invalid_grant" in detail:
            sys.exit(
                "The client credentials are FINE — the authorization code was\n"
                "rejected. In order of likelihood:\n"
                "  1. The code was already used, or expired. Codes are\n"
                "     single-use and short-lived: re-run --url, approve, and\n"
                "     exchange within a minute.\n"
                f"  2. The OAuth client's 'Redirect URLs' field does not contain\n"
                f"     exactly: {REDIRECT_URI}\n"
                "     Zendesk reports a redirect_uri mismatch as invalid_grant\n"
                "     too. Check that field before retrying.\n"
                "  3. The code was truncated on paste (they are long)."
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


def main(argv: list[str]) -> None:
    env = load_env()
    if len(argv) != 1:
        sys.exit(__doc__)

    if argv[0] in {"--url", "-u"}:
        subdomain, client_id = require(env, "ZENDESK_SUBDOMAIN", "ZENDESK_OAUTH_CLIENT_ID")
        print("Open this in a browser logged into Zendesk, approve, then copy the")
        print("`code=` value out of the URL you land on:\n")
        print(authorize_url(subdomain, client_id))
        print(f"\nThen immediately:  uv run python {Path(__file__).name} <code>")
        return

    subdomain, client_id, client_secret = require(
        env, "ZENDESK_SUBDOMAIN", "ZENDESK_OAUTH_CLIENT_ID", "ZENDESK_OAUTH_CLIENT_SECRET"
    )
    token = exchange(subdomain, client_id, client_secret, argv[0].strip())
    write_token(token)
    print(f"Success. ZENDESK_OAUTH_TOKEN written to .env ({len(token)} chars, not printed).")
    print("Next: docs/zendesk-runbook.md step 3 for ZENDESK_AI_USER_ID.")


if __name__ == "__main__":
    main(sys.argv[1:])
