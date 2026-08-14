"""HMAC-SHA256 signature verification for Zendesk webhook requests.

Zendesk's real signing scheme (see docs/zendesk-runbook.md, "Webhook +
signing secret") signs `timestamp + raw_body` — not the parsed-then-
re-serialized JSON. Re-serializing before hashing changes the bytes (key
order, whitespace, unicode escaping) and silently breaks verification
against a real webhook the moment Zendesk's wire format doesn't match
whatever `json.dumps` would produce, so every caller here MUST pass the
exact bytes read off the request body.

The signing secret is used DIRECTLY as the HMAC key — it is NOT
base64-decoded first. An earlier revision decoded it, which made every
real request fail closed with 401 ("signing secret is not valid base64")
before the signature was even compared: Zendesk's actual secret is a
44-character string that is not valid base64. The unit tests did not catch
this because they minted their own base64-valid fake secret; only first
contact with a real webhook exposed it. Zendesk's docs specify only
"sign the body and signature timestamp with the webhook secret key using
SHA256, then base64 encoding the resulting digest" — the base64 applies to
the digest, not the key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac


class SignatureVerificationError(Exception):
    """Raised when a webhook request's signature is missing or invalid."""


def compute_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Return the base64-encoded HMAC-SHA256 signature for `timestamp +
    raw_body`, keyed by the signing secret's own bytes.
    """
    key = secret.encode("utf-8")
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
