"""The embedder seam (ADR-008 / BUILD-PLAN §1.4) — offline, no key, no network.

Every Voyage request here is served by ``respx`` over a real ``httpx.Client``,
so ``VoyageEmbedder`` builds and parses genuine HTTP the same way
``backend/tests/contract/_fake_zendesk.py`` exercises ``ZendeskAdapter``. The
one thing this cannot prove is that Voyage's live API agrees with the fake —
that was measured separately during W2-B and is recorded in
``VoyageEmbedder.min_score`` and the work-package report, and is exactly why
``VOYAGE_API_KEY`` never appears in this file.

No test here requires ``VOYAGE_API_KEY`` or reaches the network, and no test
here needs the database. That is deliberate: the gated ``-m "not live"``
suite must stay runnable on CI, which has neither.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import httpx
import pytest
import respx

from data.embeddings import (
    EMBEDDER_ENV_VAR,
    EMBEDDING_DIM,
    VOYAGE_API_KEY_ENV_VAR,
    VOYAGE_API_URL,
    VOYAGE_MODEL,
    HashingEmbedder,
    VoyageEmbedder,
    VoyageEmbeddingError,
    default_embedder,
    default_min_score,
    min_score_for,
)

FAKE_KEY = "pa-test-not-a-real-key"


def _payload(*vectors: list[float]) -> dict[str, object]:
    return {
        "object": "list",
        "model": VOYAGE_MODEL,
        "data": [
            {"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)
        ],
        "usage": {"total_tokens": 4},
    }


def _vector(fill: float = 0.1, dim: int = EMBEDDING_DIM) -> list[float]:
    return [fill] * dim


# -- request construction ----------------------------------------------------


@respx.mock
def test_request_pins_the_model_and_passes_output_dimension_explicitly() -> None:
    """BUILD-PLAN §1.4 pins ``voyage-4-lite`` with ``output_dimension=1024``
    passed EXPLICITLY, precisely so a change to Voyage's own default cannot
    silently start producing vectors the ``vector(1024)`` column rejects."""
    route = respx.post(VOYAGE_API_URL).mock(
        return_value=httpx.Response(200, json=_payload(_vector()))
    )

    VoyageEmbedder(api_key=FAKE_KEY).embed(["hello"])

    request = route.calls.last.request
    sent = json.loads(request.content)
    assert sent["model"] == "voyage-4-lite"
    assert sent["output_dimension"] == 1024
    assert sent["input"] == ["hello"]
    assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"


@respx.mock
def test_input_type_is_sent_only_when_asked_for() -> None:
    """Asymmetric retrieval: the seeder indexes with ``document`` and
    ``search_kb`` queries with ``query``. An embedder with no hint must not
    send the field at all rather than sending a wrong default."""
    route = respx.post(VOYAGE_API_URL).mock(
        return_value=httpx.Response(200, json=_payload(_vector()))
    )

    VoyageEmbedder(api_key=FAKE_KEY, input_type="document").embed(["a"])
    assert json.loads(route.calls.last.request.content)["input_type"] == "document"

    VoyageEmbedder(api_key=FAKE_KEY, input_type="query").embed(["a"])
    assert json.loads(route.calls.last.request.content)["input_type"] == "query"

    VoyageEmbedder(api_key=FAKE_KEY).embed(["a"])
    assert "input_type" not in json.loads(route.calls.last.request.content)


@respx.mock
def test_long_inputs_are_split_into_batches_and_rejoined_in_order() -> None:
    """The free tier's 10k-tokens-per-minute ceiling makes a whole-KB batch
    impossible, so ``embed`` chunks the input. The rejoin must preserve
    order — ``data.seed._seed_kb`` zips the returned vectors against its rows
    with ``strict=True``, so a reordering here would attach every chunk's
    text to a different chunk's vector and be silently, catastrophically
    wrong rather than raise."""
    responses = [
        httpx.Response(200, json=_payload(_vector(0.1), _vector(0.2))),
        httpx.Response(200, json=_payload(_vector(0.3))),
    ]
    respx.post(VOYAGE_API_URL).mock(side_effect=responses)

    vectors = VoyageEmbedder(api_key=FAKE_KEY, batch_size=2).embed(["a", "b", "c"])

    assert [v[0] for v in vectors] == [0.1, 0.2, 0.3]


@respx.mock
def test_out_of_order_response_is_re_sorted_by_index() -> None:
    """Voyage documents that ``index`` mirrors input order. This does not
    take that on faith, because a mis-ordered reseed is silently wrong."""
    payload = _payload(_vector(0.1), _vector(0.2))
    data = payload["data"]
    assert isinstance(data, list)
    payload["data"] = list(reversed(data))
    respx.post(VOYAGE_API_URL).mock(return_value=httpx.Response(200, json=payload))

    vectors = VoyageEmbedder(api_key=FAKE_KEY).embed(["a", "b"])

    assert [v[0] for v in vectors] == [0.1, 0.2]


# -- failure handling --------------------------------------------------------


@respx.mock
def test_rate_limited_request_is_retried_and_then_succeeds() -> None:
    """This project's Voyage account has no payment method and therefore
    sits on the free tier's 3 RPM / 10k TPM limits — measured live, and the
    reason a reseed 429s repeatedly rather than occasionally. Without this
    retry, seeding simply fails."""
    respx.post(VOYAGE_API_URL).mock(
        side_effect=[
            httpx.Response(429, json={"detail": "rate limited"}),
            httpx.Response(200, json=_payload(_vector(0.5))),
        ]
    )
    waits: list[float] = []

    vectors = VoyageEmbedder(api_key=FAKE_KEY, sleep=waits.append).embed(["a"])

    assert vectors[0][0] == 0.5
    assert waits, "a 429 must actually back off, not spin"


@respx.mock
def test_persistent_rate_limiting_raises_with_the_providers_own_message() -> None:
    respx.post(VOYAGE_API_URL).mock(
        return_value=httpx.Response(429, json={"detail": "still rate limited"})
    )

    with pytest.raises(VoyageEmbeddingError, match="still rate limited"):
        VoyageEmbedder(api_key=FAKE_KEY, sleep=lambda _: None).embed(["a"])


@respx.mock
def test_authentication_failure_is_not_retried() -> None:
    """A 401 will not fix itself; retrying it eight times just delays the
    error by minutes at free-tier wait times."""
    route = respx.post(VOYAGE_API_URL).mock(
        return_value=httpx.Response(401, json={"detail": "bad key"})
    )

    with pytest.raises(VoyageEmbeddingError, match="401"):
        VoyageEmbedder(api_key=FAKE_KEY, sleep=lambda _: None).embed(["a"])

    assert route.call_count == 1


@respx.mock
def test_wrong_dimension_is_rejected_rather_than_written_to_the_column() -> None:
    """``kb_chunks.embedding`` is ``vector(1024)``. A 512-wide vector would
    fail at INSERT with a Postgres error naming neither the model nor the
    dimension asked for; failing here says exactly what went wrong."""
    respx.post(VOYAGE_API_URL).mock(
        return_value=httpx.Response(200, json=_payload(_vector(0.1, dim=512)))
    )

    with pytest.raises(VoyageEmbeddingError, match="512-dimension"):
        VoyageEmbedder(api_key=FAKE_KEY).embed(["a"])


@respx.mock
def test_short_response_is_rejected() -> None:
    """Fewer vectors than inputs would make ``_seed_kb``'s ``zip(...,
    strict=True)`` raise somewhere unrelated."""
    respx.post(VOYAGE_API_URL).mock(
        return_value=httpx.Response(200, json=_payload(_vector()))
    )

    with pytest.raises(VoyageEmbeddingError, match="1 embeddings for 2 inputs"):
        VoyageEmbedder(api_key=FAKE_KEY).embed(["a", "b"])


def test_missing_api_key_fails_loudly_instead_of_degrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent fallback to the lexical embedder would seed the KB in one
    embedding space and query it in another — plausible-looking nonsense
    instead of an error."""
    monkeypatch.delenv(VOYAGE_API_KEY_ENV_VAR, raising=False)

    with pytest.raises(VoyageEmbeddingError, match=VOYAGE_API_KEY_ENV_VAR):
        VoyageEmbedder()


# -- default resolution ------------------------------------------------------


def test_default_is_the_offline_lexical_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI has no network and no key. The default must fail *closed* into
    offline, and must NOT be "use Voyage if VOYAGE_API_KEY happens to be
    set" — this repo's .env carries the key, so that rule would put the
    gated suite on the network the moment someone sourced it."""
    monkeypatch.delenv(EMBEDDER_ENV_VAR, raising=False)
    monkeypatch.setenv(VOYAGE_API_KEY_ENV_VAR, FAKE_KEY)

    assert isinstance(default_embedder(), HashingEmbedder)


def test_voyage_is_selected_only_by_the_explicit_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(EMBEDDER_ENV_VAR, "voyage")
    monkeypatch.setenv(VOYAGE_API_KEY_ENV_VAR, FAKE_KEY)

    embedder = default_embedder(input_type="document")

    assert isinstance(embedder, VoyageEmbedder)
    assert embedder.input_type == "document"
    assert embedder.dim == EMBEDDING_DIM


def test_an_unknown_embedder_name_is_an_error_not_a_silent_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo (``KB_EMBEDDER=voyageai``) that quietly seeded with the lexical
    embedder is the exact class of defect that let this stack run for weeks
    with no ANTHROPIC_API_KEY."""
    monkeypatch.setenv(EMBEDDER_ENV_VAR, "voyageai")

    with pytest.raises(ValueError, match="voyageai"):
        default_embedder()


# -- the relevance floor travels with the embedding space --------------------


def test_each_embedder_carries_its_own_calibrated_floor() -> None:
    """ADR-010: the floor is a property of the score distribution, so it
    cannot be one shared constant. These two differ by a factor of ~3 and a
    single value would either be a no-op for one or reject everything from
    the other."""
    assert min_score_for(HashingEmbedder()) == HashingEmbedder.min_score
    assert HashingEmbedder.min_score != VoyageEmbedder.min_score
    assert 0 < HashingEmbedder.min_score < VoyageEmbedder.min_score


def test_an_uncalibrated_injected_embedder_gets_no_floor() -> None:
    """``Embedder`` is frozen at ``dim``/``embed`` (§1.4), so an injected
    implementation cannot be required to declare a floor. Applying somebody
    else's calibration to an unknown embedding space would be worse than
    applying none."""

    class Custom:
        dim = 3

        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            return [[0.0, 0.0, 1.0] for _ in texts]

    assert min_score_for(Custom()) == 0.0


def test_default_min_score_follows_the_configured_embedder_without_building_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``agent.config.KB_MIN_SCORE`` is resolved at import time. Constructing
    a ``VoyageEmbedder`` to read its floor would open an httpx client and
    demand an API key as a side effect of importing a config module — so the
    value is read off the class, and this proves it (no key is set here)."""
    monkeypatch.delenv(VOYAGE_API_KEY_ENV_VAR, raising=False)

    monkeypatch.setenv(EMBEDDER_ENV_VAR, "hashing")
    assert default_min_score() == HashingEmbedder.min_score

    monkeypatch.setenv(EMBEDDER_ENV_VAR, "voyage")
    assert default_min_score() == VoyageEmbedder.min_score
