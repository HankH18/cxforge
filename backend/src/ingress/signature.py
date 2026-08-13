"""HMAC-SHA256 signature verification for Zendesk webhook requests.

Zendesk's real signing scheme (see docs/zendesk-runbook.md, "Webhook +
signing secret") signs `timestamp + raw_body` — not the parsed-then-
re-serialized JSON. Re-serializing before hashing changes the bytes (key
order, whitespace, unicode escaping) and silently breaks verification
against a real webhook the moment Zendesk's wire format doesn't match
whatever `json.dumps` would produce, so every caller here MUST pass the
exact bytes read off the request body.

The signing secret Zendesk issues when the webhook is created is itself
base64-encoded and must be base64-decoded before use as the HMAC key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac


class SignatureVerificationError(Exception):
    """Raised when a webhook request's signature is missing or invalid."""


def compute_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Return the base64-encoded HMAC-SHA256 signature for `timestamp +
    raw_body`, keyed by the base64-encoded signing secret Zendesk issued.
    """
    try:
        key = base64.b64decode(secret, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignatureVerificationError("signing secret is not valid base64") from exc
    message = timestamp.encode("utf-8") + raw_body
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def verify_signature(
    *,
    secret: str,
    timestamp: str | None,
    signature: str | None,
    raw_body: bytes,
) -> None:
    """Verify a Zendesk webhook request against the exact raw body bytes.

    Raises `SignatureVerificationError` if either header is missing or the
    signature doesn't match. Comparison uses `hmac.compare_digest` — a
    plain `==` short-circuits on the first mismatched byte, which leaks
    timing information an attacker can use to forge a valid signature one
    byte at a time.
    """
    if not timestamp or not signature:
        raise SignatureVerificationError("missing signature headers")
    expected = compute_signature(secret, timestamp, raw_body)
    if not hmac.compare_digest(expected, signature):
        raise SignatureVerificationError("signature mismatch")
