"""Webhook ingress: receipt, HMAC verification, idempotency, self-event drop.

Scaffold only — T-4 implements the endpoint on this router.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/webhooks", tags=["ingress"])
