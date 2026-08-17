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
import logging
import os

import pytest
from fastapi.testclient import TestClient

from data import get_connection

from .conftest import (
    TEST_AI_USER_ID,
    TEST_SIGNING_SECRET,
    RecordingJobQueue,
    unique_ticket_id,
)

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


# -- the comment-id placeholder defect (2026-08-17) -----------------------
#
# Measured live, from Zendesk's own `/api/v2/webhooks/{id}/invocations`
# record of what it actually sent to this endpoint. The trigger's original
# `{{ticket.latest_comment_id}}` placeholder does not exist in this account
# and renders as the empty string, so every delivery arrived with
# `"comment_id": ""`. Two verbatim examples, invocations
# 01M079ZXEVECSTHCAP7EAP0SVA (07:29:19Z) and 01M07AA00MZJ23QBBR5MKZWDEM
# (07:34:48Z), both for ticket 3:
#
#     "comment_id": ""
#
# and after the trigger was changed to `{{ticket.latest_comment.id}}`,
# invocations 01M07AMDNSHP8D7584XEZDTQMP and 01M07B1QPS4J2TQJSDNPP6ZR65 —
# two different customer comments on the SAME ticket 3:
#
#     "comment_id": "54509363035291"
#     "comment_id": "54509451282203"
#
# The ticket_id below is namespaced by the `ticket_id` fixture instead of
# the literal "3" so the suite stays re-runnable and cleans up after
# itself; the comment ids are the real ones.

REAL_COMMENT_ID_A = "54509363035291"
REAL_COMMENT_ID_B = "54509451282203"


def test_empty_comment_id_is_rejected_rather_than_becoming_a_dedup_key(
    client: TestClient, ticket_id: str, job_queue: RecordingJobQueue
) -> None:
    """The defect itself. `comment_id: ""` used to validate, so every comment
    on a ticket collapsed onto the key `(N, "")`: the first was processed and
    every follow-up was silently discarded as a duplicate.

    An id that identifies nothing must not become half of the idempotency
    key. Rejecting is the loud answer — it lands in Zendesk's webhook
    activity log and in ingress's ERROR stream — where accepting degrades
    into "the customer gets one answer and then silence".
    """
    body = _payload_bytes(ticket_id=ticket_id, comment_id="")

    response = client.post(URL, content=body, headers=_headers(body))

    assert response.status_code == 400
    # The 400 must say what to go and fix, not just "invalid".
    assert "{{ticket.latest_comment.id}}" in response.text
    assert "zendesk-runbook.md" in response.text
    # Nothing was written, so nothing is poisoned for the next delivery.
    assert _seen_count(ticket_id, "") == 0
    assert job_queue.enqueued == []


def test_whitespace_only_comment_id_is_rejected_too(
    client: TestClient, ticket_id: str
) -> None:
    """A placeholder that renders as a space is exactly as useless as one
    that renders as nothing, and would otherwise sail past a `!= ""` check."""
    body = _payload_bytes(ticket_id=ticket_id, comment_id="   ")

    response = client.post(URL, content=body, headers=_headers(body))

    assert response.status_code == 400
    assert _seen_count(ticket_id, "   ") == 0


def test_empty_ticket_id_is_rejected(client: TestClient) -> None:
    """The other half of the primary key, for the same reason."""
    body = _payload_bytes(ticket_id="", comment_id="c-1")

    response = client.post(URL, content=body, headers=_headers(body))

    assert response.status_code == 400
    assert "{{ticket.id}}" in response.text


def test_two_real_zendesk_deliveries_on_one_ticket_both_dispatch(
    client: TestClient, ticket_id: str, job_queue: RecordingJobQueue
) -> None:
    """The behaviour the empty placeholder broke, replayed with the comment
    ids Zendesk really delivered once the placeholder was fixed.

    Under `{{ticket.latest_comment_id}}` both of these bodies carried
    `comment_id: ""`, so the second answered `duplicate: true` and enqueued
    nothing — the customer's follow-up was thrown away.
    """
    first_body = _payload_bytes(
        ticket_id=ticket_id,
        comment_id=REAL_COMMENT_ID_A,
        latest_comment_text="could you tell me what stage case MFG-2025-0301 is at?",
    )
    second_body = _payload_bytes(
        ticket_id=ticket_id,
        comment_id=REAL_COMMENT_ID_B,
        latest_comment_text="follow-up on the same ticket — is the DNA profile available?",
    )

    first = client.post(URL, content=first_body, headers=_headers(first_body))
    second = client.post(URL, content=second_body, headers=_headers(second_body))

    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is False

    # Two distinct dedup keys, and two dispatched runs — not one of each.
    assert _seen_count(ticket_id, REAL_COMMENT_ID_A) == 1
    assert _seen_count(ticket_id, REAL_COMMENT_ID_B) == 1
    dispatched = [(job.ticket_id, job.comment_id) for job in job_queue.enqueued]
    assert dispatched == [
        (ticket_id, REAL_COMMENT_ID_A),
        (ticket_id, REAL_COMMENT_ID_B),
    ]


def test_a_discarded_duplicate_delivery_is_not_silent(
    client: TestClient, ticket_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Discarding a duplicate is correct; doing it invisibly is what let the
    defect hide. A dropped delivery must leave a log line naming the key it
    collapsed onto."""
    body = _payload_bytes(ticket_id=ticket_id, comment_id="c-dup")
    client.post(URL, content=body, headers=_headers(body))

    with caplog.at_level(logging.WARNING, logger="ingress"):
        second = client.post(URL, content=body, headers=_headers(body))

    assert second.json()["duplicate"] is True
    discards = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "duplicate" in record.getMessage()
    ]
    assert len(discards) == 1
    message = discards[0].getMessage()
    assert ticket_id in message
    assert "c-dup" in message


def test_a_rejected_delivery_is_not_silent(
    client: TestClient, ticket_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A 4xx'd delivery is a customer event Zendesk will never retry, so it
    belongs in the ERROR stream rather than only in Zendesk's admin UI."""
    body = _payload_bytes(ticket_id=ticket_id, comment_id="")

    with caplog.at_level(logging.ERROR, logger="ingress"):
        client.post(URL, content=body, headers=_headers(body))

    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "comment_id" in errors[0].getMessage()


def test_rejection_log_does_not_leak_the_request_body(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Control on the log line above: the raw body carries the requester's
    email and comment text, and a pydantic error dict embeds its input by
    default. The log line must not."""
    body = b"this is not json at all"

    with caplog.at_level(logging.ERROR, logger="ingress"):
        response = client.post(URL, content=body, headers=_headers(body))

    assert 400 <= response.status_code < 500
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "this is not json at all" not in logged
