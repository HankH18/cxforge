"""GET|PUT /api/settings/gate (R11) — pinned response shape ``{enabled:
bool}``, and confirmation the written value lands in the EXACT
``settings.key`` T-5's ``agent.nodes.decide`` reads via
``agent.store.read_gate_enabled`` (``agent.config.GATE_SETTING_KEY``) —
not a parallel key only the portal itself understands.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent.store import read_gate_enabled

from .conftest import AUTH_HEADERS


def test_gate_defaults_to_disabled_when_no_row_exists(client: TestClient) -> None:
    response = client.get("/api/settings/gate", headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.json() == {"enabled": False}
    assert read_gate_enabled() is False  # same key, read via T-5's own path


def test_put_then_get_round_trips_through_the_same_settings_key(client: TestClient) -> None:
    put_on = client.put("/api/settings/gate", json={"enabled": True}, headers=AUTH_HEADERS)
    assert put_on.status_code == 200
    assert put_on.json() == {"enabled": True}

    get_on = client.get("/api/settings/gate", headers=AUTH_HEADERS)
    assert get_on.status_code == 200
    assert get_on.json() == {"enabled": True}

    # The value the portal wrote is visible through agent.store's own read
    # path (agent.nodes.decide's gate check) — the round trip T-5's decide
    # node depends on for the toggle to actually affect agent behavior.
    assert read_gate_enabled() is True

    put_off = client.put("/api/settings/gate", json={"enabled": False}, headers=AUTH_HEADERS)
    assert put_off.status_code == 200
    assert put_off.json() == {"enabled": False}

    get_off = client.get("/api/settings/gate", headers=AUTH_HEADERS)
    assert get_off.status_code == 200
    assert get_off.json() == {"enabled": False}
    assert read_gate_enabled() is False
