"""FastAPI application root.

Mounts one router per component package. The routers are declared empty in
T-0 and filled in by the ticket that owns each package (ingress → T-4,
portal → T-8), so this file never needs editing outside T-0's scope.
"""

from fastapi import FastAPI

from ingress import router as ingress_router
from portal import router as portal_router

app = FastAPI(title="Othram AI Support Agent", version="0.1.0")

app.include_router(ingress_router)
app.include_router(portal_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe, used by the deploy verification script (T-11)."""
    return {"status": "ok"}
