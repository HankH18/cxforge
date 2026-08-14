"""POST /webhooks/zendesk (DESIGN §Webhook ingress, SPEC R1).

Requires the docker-compose Postgres (`tickets_seen`), so this whole module
skips itself when SKIP_DB_TESTS=1 (CI has no db service), mirroring
backend/tests/data/*'s convention.

Signatures in this file are computed independently of
`ingress.signature.compute_signature` (plain hmac/hashlib/base64 calls
below) so a bug in that function can't be masked by tests that build their
expected value with the same buggy code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

import pytest
from fastapi.testclient import TestClient

from data import get_connection

from .conftest import TEST_AI_USER_ID, TEST_SIGNING_SECRET, unique_ticket_id

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_DB_TESTS") == "1",
    reason="requires the docker-compose db service (CI sets SKIP_DB_TESTS=1)",
)

URL = "/webhooks/zendesk"
TIMESTAMP = "2026-08-13T12:00:00Z"
SIGNATURE_HEADER = "X-Zendesk-Webhook-Signature"
TIMESTAMP_HEADER = "X-Zendesk-Webhook-Signature-Timestamp"


def _sign(raw_body: bytes, *, timestamp: str = TIMESTAMP, secret: str = TEST_SIGNING_SECRET) -> str:
    """Independent reimplementation of Zendesk's signing scheme.

    The secret's OWN BYTES are the HMAC key — it is not base64-decoded
    first. Only the resulting digest is base64-encoded. Verified against a
    live Zendesk webhook; the previous decode-the-secret version of this
    helper matched a bug in the implementation rather than reality, so both
    agreed with each other and neither agreed with Zendesk.
    """
    key = secret.encode("utf-8")
    digest = hmac.new(key, timestamp.encode("utf-8") + raw_body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _headers(
    raw_body: bytes, *, timestamp: str = TIMESTAMP, secret: str = TEST_SIGNING_SECRET
) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: _sign(raw_body, timestamp=timestamp, secret=secret),
        TIMESTAMP_HEADER: timestamp,
    }


def _payload_bytes(
    *,
    ticket_id: str,
    comment_id: str,
    requester_email: str = "customer@example.com",
    subject: str = "Where is my case?",
    latest_comment_text: str = "Any update?",
    comment_author_id: str | None = None,
) -> bytes:
    body: dict[str, str] = {
        "ticket_id": ticket_id,
        "comment_id": comment_id,
        "requester_email": requester_email,
        "subject": subject,
        "latest_comment_text": latest_comment_text,
    }
    if comment_author_id is not None:
        body["comment_author_id"] = comment_author_id
    return json.dumps(body).encode("utf-8")


def _seen_count(ticket_id: str, comment_id: str) -> int:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM tickets_seen WHERE ticket_id = %s AND comment_id = %s",
            (ticket_id, comment_id),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


# -- signature verification -------------------------------------------------


def test_valid_signature_accepted(client: TestClient, ticket_id: str) -> None:
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")

    response = client.post(URL, content=body, headers=_headers(body))

    assert response.status_code == 202
    assert response.json()["duplicate"] is False
    assert _seen_count(ticket_id, "c-1") == 1


def test_missing_signature_rejected(client: TestClient, ticket_id: str) -> None:
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")

    response = client.post(URL, content=body, headers={"Content-Type": "application/json"})

    assert 400 <= response.status_code < 500
    assert _seen_count(ticket_id, "c-1") == 0


def test_missing_timestamp_header_rejected(client: TestClient, ticket_id: str) -> None:
    """Only the signature header present, no timestamp — still rejected,
    not silently verified against a missing/empty timestamp."""
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")

    response = client.post(
        URL,
        content=body,
        headers={
            "Content-Type": "application/json",
            SIGNATURE_HEADER: _sign(body),
        },
    )

    assert 400 <= response.status_code < 500
    assert _seen_count(ticket_id, "c-1") == 0


def test_bad_signature_rejected(client: TestClient, ticket_id: str) -> None:
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")
    headers = _headers(body)
    headers[SIGNATURE_HEADER] = base64.b64encode(b"not-the-right-digest-bytes!!").decode("utf-8")

    response = client.post(URL, content=body, headers=headers)

    assert 400 <= response.status_code < 500
    assert _seen_count(ticket_id, "c-1") == 0


def test_signature_verified_over_raw_body_not_reserialized_json(
    client: TestClient, ticket_id: str
) -> None:
    """Hand-build a body with unusual whitespace and non-model-order keys.
    `json.dumps` of the parsed dict would produce different bytes (no
    newlines/extra spaces, keys in insertion order) — if the endpoint ever
    re-serializes the parsed JSON before checking the signature instead of
    hashing the exact bytes received, the signature computed here (over the
    raw bytes) would no longer match and this request would be wrongly
    rejected.
    """
    raw_body = (
        b'{\n  "subject" : "weird   spacing",\n'
        b'"comment_id":"c-raw",\n'
        b'  "ticket_id": "' + ticket_id.encode("utf-8") + b'",\n'
        b'"latest_comment_text" :"hi there",\n'
        b'"requester_email":  "customer@example.com"\n}'
    )

    response = client.post(URL, content=raw_body, headers=_headers(raw_body))

    assert response.status_code == 202
    assert _seen_count(ticket_id, "c-raw") == 1


# -- idempotency --------------------------------------------------------


def test_duplicate_ticket_comment_pair_is_a_noop(client: TestClient, ticket_id: str) -> None:
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")

    first = client.post(URL, content=body, headers=_headers(body))
    second = client.post(URL, content=body, headers=_headers(body))

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert _seen_count(ticket_id, "c-1") == 1


def test_different_comment_id_same_ticket_is_processed(client: TestClient, ticket_id: str) -> None:
    """Proves the idempotency key is the (ticket_id, comment_id) PAIR, not
    just ticket_id: a second, genuinely new comment on the same ticket must
    NOT be treated as a duplicate."""
    first_body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1")
    second_body = _payload_bytes(ticket_id=ticket_id, comment_id="c-2")

    first = client.post(URL, content=first_body, headers=_headers(first_body))
    second = client.post(URL, content=second_body, headers=_headers(second_body))

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is False
    assert _seen_count(ticket_id, "c-1") == 1
    assert _seen_count(ticket_id, "c-2") == 1


# -- self-event drop ------------------------------------------------------


def test_self_authored_event_dropped(client: TestClient, ticket_id: str) -> None:
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-1", comment_author_id=TEST_AI_USER_ID)

    response = client.post(URL, content=body, headers=_headers(body))

    assert response.status_code == 202
    assert response.json()["status"] == "dropped"
    # Dropped, not merely deduped later: never written to tickets_seen.
    assert _seen_count(ticket_id, "c-1") == 0


def test_customer_authored_event_is_not_dropped(client: TestClient, ticket_id: str) -> None:
    """Control for the drop test above: an author id that ISN'T the AI
    user must be processed normally."""
    body = _payload_bytes(
        ticket_id=ticket_id, comment_id="c-1", comment_author_id="some-customer-id"
    )

    response = client.post(URL, content=body, headers=_headers(body))

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert _seen_count(ticket_id, "c-1") == 1


# -- malformed payload ------------------------------------------------------


def test_malformed_payload_is_4xx_not_500(client: TestClient) -> None:
    tid = unique_ticket_id()
    try:
        # Missing every pinned field except ticket_id.
        body = json.dumps({"ticket_id": tid}).encode("utf-8")

        response = client.post(URL, content=body, headers=_headers(body))

        assert 400 <= response.status_code < 500
    finally:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM tickets_seen WHERE ticket_id = %s", (tid,))


def test_non_json_body_is_4xx_not_500(client: TestClient) -> None:
    body = b"this is not json at all"

    response = client.post(URL, content=body, headers=_headers(body))

    assert 400 <= response.status_code < 500
