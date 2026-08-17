"""Embedding layer.

``Embedder`` is a narrow Protocol (ADR-008 / BUILD-PLAN §1.4 keep its shape
frozen) so more than one embedding backend can sit behind the same seam
without touching a caller. Two implementations ship:

- ``HashingEmbedder`` — deterministic, fully offline, lexical. It is the
  **default**, and deliberately so: CI has no network and no key, and the
  ``-m "not live"`` suite must never make an API call or depend on a secret.
- ``VoyageEmbedder`` — real semantic embeddings from Voyage AI
  (``voyage-4-lite``, ``output_dimension=1024``). Selected only when
  ``KB_EMBEDDER=voyage`` is set in the environment.

**The seeder and the searcher must agree.** A vector index is only
meaningful when the query vector comes from the same embedding space as the
stored ones, so ``data.seed.seed_all`` and ``data.retrieval.search_kb`` both
resolve their default through ``default_embedder()`` here rather than each
naming a class. Flipping ``KB_EMBEDDER`` therefore flips both halves at
once, and a reseed is required after flipping it (the old vectors are not
comparable to the new query vectors — they are not even in the same space).

**Relevance floors live here, not with the caller.** A cosine-similarity
cutoff is a property of the embedding space that produced the score, not of
retrieval in the abstract: ``HashingEmbedder``'s correct in-domain hits land
in a completely different band from ``VoyageEmbedder``'s. So each class
carries its own calibrated ``min_score``, measured against the same held-out
query set (see each class's docstring for the numbers and
``docs/DECISIONS.md`` ADR-010 for why a floor exists at all).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from typing import Literal, Protocol

import httpx
from sklearn.feature_extraction.text import HashingVectorizer
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_fixed

# Fixed width so it maps directly onto the `kb_chunks.embedding vector(N)`
# column — the whole reason to prefer HashingVectorizer over a fitted
# vectorizer (e.g. TfidfVectorizer), whose vocabulary size varies with the
# corpus. `voyage-4-lite` takes a configurable output_dimension and is
# pinned to this same 1024, so switching embedders is a reseed with NO
# schema migration (ADR-008).
EMBEDDING_DIM = 1024

# Which embedder `default_embedder()` hands out. Read from the environment
# rather than pinned as a constant because production and CI genuinely need
# different answers, and the default is the offline one so that "forgot to
# configure it" fails closed into no-network rather than into a live API
# call. NOTE: this is deliberately NOT "use Voyage if VOYAGE_API_KEY is
# set" — this repo's .env carries the key, so that rule would silently put
# the gated suite on the network the moment someone ran it with
# `set -a; source .env`.
#
# WHY THE READ SITES SPELL THE NAME OUT AGAIN INSTEAD OF USING THIS
# CONSTANT. `backend/tests/deploy/test_env_forwarding.py` derives the set of
# variables every application container must be given by an AST scan for
# `os.environ`/`os.getenv` keys that are STRING LITERALS. A read through a
# constant is unresolvable to it: the variable lands in that module's ledger
# of what it cannot see, and nothing then requires it in a compose file. So
# `_configured_name()` below reads `os.environ.get("KB_EMBEDDER", ...)`
# literally, which makes the variable required in every container and
# un-exemptable (`test_the_exemption_ledger_can_never_silence_a_real_
# requirement`) and forces a `.env.example` line. Do not "tidy" the literal
# back into the constant — that silently removes KB_EMBEDDER from the deploy
# audit. The two cannot drift apart unnoticed either: the tests in
# `backend/tests/data/test_embeddings.py` set the environment through these
# constants and assert on which embedder comes back, so renaming one without
# the other turns that suite red.
EMBEDDER_ENV_VAR = "KB_EMBEDDER"
DEFAULT_EMBEDDER_NAME = "hashing"

# Voyage asymmetric-retrieval hint. Documents and queries are embedded with
# different `input_type` values so a short question lands nearer the passage
# that answers it; `None` means "no hint", which is what HashingEmbedder
# (which has no such notion) effectively does.
InputType = Literal["document", "query"]


class Embedder(Protocol):
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one fixed-width, L2-normalized vector per input text."""
        ...


class HashingEmbedder:
    """Deterministic lexical embedder: sklearn ``HashingVectorizer`` + L2 norm.

    Stateless by construction — ``HashingVectorizer.transform`` never fits on
    a corpus (no vocabulary, no IDF weights), so the vector produced for a
    given string is identical across processes, runs, and seed order. Word
    unigrams+bigrams with English stopwords removed keep short lexical
    queries ("turnaround time for sequencing") aligned with chunk content
    despite the hashing trick's collision noise.
    """

    # Calibrated 2026-08-16 (ADR-010) against the 12 held-out queries in
    # backend/tests/data/test_retrieval.py plus 7 off-domain probes: the two
    # `esc-low_confidence-empty_retrieval-*` labeled bodies and 5 plainly
    # unrelated questions (weather / recipe / stocks / Python / football).
    # Measured over the 44-chunk fixture KB:
    #
    #   correct doc's BEST chunk, 12 held-out   0.1068 .. 0.3762
    #   off-domain top-1, 7 probes              0.0542 .. 0.2238
    #
    # **These bands OVERLAP**, and that is the honest headline for this
    # embedder: "Who won the World Cup final in 2022?" scores 0.2238 against
    # refund-policy — higher than the correct answer for 5 of the 12 genuine
    # queries — because hashed bigram collisions are noise, not meaning. No
    # cutoff can separate signal from noise here.
    #
    # 0.09 is therefore a *garbage filter*, not a relevance floor, and the
    # window it must sit in is only 0.03 wide:
    #
    #   > 0.0761  or `esc-low_confidence-empty_retrieval-accreditation-01`
    #             still retrieves, and R6's empty_retrieval trigger stays
    #             unreachable — the whole point of ADR-010.
    #   <= 0.1068 or "what do I actually get at the end" loses
    #             results-delivery-and-formats entirely and
    #             test_retrieval_surfaces_expected_doc_in_top_3 goes red. A
    #             floor that discards a correct answer is not a floor.
    #
    # 0.09 is the midpoint. It rejects 4 of the 7 off-domain probes; it
    # cannot reject the football question (0.2238), the international-
    # shipping ticket (0.1320) or the Python question (0.0948), and raising
    # it far enough to would start discarding correct answers. See
    # VoyageEmbedder for the contrast: real semantic embeddings open a clean
    # gap between the two bands instead of a 0.03 window.
    #
    # Second measured limit, and the reason this embedder should not be what
    # production searches with: `agent.nodes.kb_answer` queries with
    # `state["topic"]` — the classifier's paraphrase — not the customer's
    # words. Over 12 topic paraphrases of the held-out questions (one per
    # doc, deliberately using none of the doc's vocabulary): this embedder
    # gets the correct doc at rank 1 for **5 of 12** and retrieves nothing at
    # all for 3, where VoyageEmbedder gets 11 of 12 and retrieves for all 12.
    # Lowering the floor does not fix that — the correct chunk for those
    # paraphrases scores as low as 0.0290, i.e. deep inside the noise band,
    # so the chunks that survive are usually not the relevant ones. It is a
    # limit of lexical matching, not of the cutoff.
    min_score: float = 0.09

    def __init__(self, n_features: int = EMBEDDING_DIM) -> None:
        self.dim = n_features
        self._vectorizer = HashingVectorizer(
            n_features=n_features,
            norm="l2",
            alternate_sign=True,
            stop_words="english",
            ngram_range=(1, 2),
            lowercase=True,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        matrix = self._vectorizer.transform(list(texts))
        return [row.tolist() for row in matrix.toarray()]


# -- Voyage AI ---------------------------------------------------------------

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
# Spelled out literally at its read site in `VoyageEmbedder.__init__` for the
# same reason as EMBEDDER_ENV_VAR above — see that note. This one matters
# more, not less: it is the credential, and an unforwarded credential is the
# precise failure `docs/STATE.md §6.2` records.
VOYAGE_API_KEY_ENV_VAR = "VOYAGE_API_KEY"

# ADR-008 / BUILD-PLAN §1.4 pin the model and dimension. `voyage-4-lite`
# takes a configurable output_dimension (256/512/1024/2048) and 1024 is
# passed EXPLICITLY rather than relying on the API default, so the contract
# is visible at the call site and a change to Voyage's default cannot
# silently produce vectors the `vector(1024)` column rejects.
VOYAGE_MODEL = "voyage-4-lite"

# Voyage accepts up to 1000 inputs per request. 16 is far below that on
# purpose: measured live 2026-08-16, this project's Voyage account has no
# payment method attached and therefore sits on the free tier's **3 requests
# per minute / 10,000 tokens per minute** limits (the 429 body says so
# verbatim). A whole-KB batch is ~12k tokens, i.e. over the per-minute token
# ceiling in a single request, so a large batch cannot succeed at all here —
# it 429s forever rather than slowly. Small batches plus the Retry-After
# backoff below make a reseed slow (~2 min for 44 chunks) but reliable.
# Raise this once the account has a payment method; see the W2-B report.
VOYAGE_BATCH_SIZE = 16

# Free-tier 429s are per-minute, so a retry has to be willing to wait out a
# whole minute rather than give up after a few seconds of exponential
# backoff. Voyage does not send Retry-After, so this is a fixed wait.
VOYAGE_RATE_LIMIT_WAIT_SECONDS = 20.0
VOYAGE_MAX_ATTEMPTS = 8


class VoyageEmbeddingError(RuntimeError):
    """Voyage returned something unusable — a non-2xx status, or a payload
    whose shape or dimension does not match what was asked for."""


class _VoyageRetryable(Exception):
    """Internal: a 429 or 5xx, i.e. a response worth trying again. Never
    escapes ``_embed_batch`` — the last attempt's failure is re-raised as a
    ``VoyageEmbeddingError`` with the provider's own body attached."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"{status_code} {body}")
        self.status_code = status_code
        self.body = body


class VoyageEmbedder:
    """Semantic embedder over Voyage AI's ``/v1/embeddings`` endpoint.

    Speaks HTTP through ``httpx`` (already a dependency, and the same
    transport ``helpdesk.zendesk_adapter`` uses) rather than pulling in the
    ``voyageai`` SDK: the request is three fields and the response is one
    array, so the SDK would buy nothing but a new dependency and a second
    retry policy to reason about.

    Never constructed unless ``KB_EMBEDDER=voyage`` — see
    ``default_embedder``. Construction fails loudly on a missing key rather
    than degrading to the lexical embedder, because a silent downgrade would
    mean seeding the KB with vectors from one space and querying it with
    another, which produces plausible-looking nonsense instead of an error.

    Calibrated 2026-08-16 against the same 12 held-out queries and 7
    off-domain probes as ``HashingEmbedder`` — see ``min_score`` below.
    """

    # Calibrated 2026-08-16 (ADR-010) on the SAME 12 held-out queries and
    # off-domain probes as HashingEmbedder, against a KB reseeded with this
    # embedder, and additionally on *topic-shaped* queries — because
    # `agent.nodes.kb_answer` searches with `state["topic"]`, the
    # classifier's one-sentence paraphrase, not with the customer's words.
    # Measured top-1 scores:
    #
    #   correct doc's BEST chunk, customer wording (12)  0.2929 .. 0.6336
    #   correct doc's BEST chunk, topic paraphrase (12)  0.3101 .. 0.5561
    #   off-domain top-1, customer wording (5)           0.1110 .. 0.2159
    #   off-domain top-1, topic paraphrase (3)           0.2151 .. 0.2644
    #
    # (Voyage is not bit-deterministic across reseeds — the same query moved
    # by ~0.001 between two runs. The margins below are 40x that, so it does
    # not matter, but do not treat any single digit here as exact.)
    #
    # Unlike the lexical embedder those bands do not overlap: 0.2929 is the
    # worst genuine hit and 0.2644 the best false lead. 0.25 sits below
    # every genuine hit by 0.0429 — deliberately nearer the bottom of the
    # gap than the middle, because the two errors are not symmetric. A floor
    # set too low passes weak chunks to the KB answerer, which then says it
    # lacks information and the verifier escalates anyway; a floor set too
    # high escalates questions the KB genuinely answers, which is a
    # demo-visible inflation of the escalation rate (BUILD-PLAN §10.2 Gap 2)
    # and a worse outcome.
    #
    # Two honest limits, both measured rather than assumed:
    #
    # 1. The one probe 0.25 fails to reject is the topic paraphrase "the
    #    customer is asking about the weather" (0.2644) — but a weather
    #    question classifies as `off_topic` and never reaches search_kb at
    #    all, so this is an artificial lower bound, not a live gap.
    # 2. The two `esc-low_confidence-empty_retrieval-*` labeled tickets (lab
    #    accreditation; international shipping of remains) score 0.4322 and
    #    0.4132 — *inside* the in-domain band, because Voyage correctly
    #    reads them as near-neighbours of sample-submission-chain-of-custody.
    #    No floor can reject those two without also rejecting half the
    #    genuine hits. They still escalate, via the groundedness verifier
    #    rather than via empty retrieval; the labeled `expected_reasons`
    #    (`low_confidence`) is the same either way.
    min_score: float = 0.25

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = VOYAGE_MODEL,
        output_dimension: int = EMBEDDING_DIM,
        input_type: InputType | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        batch_size: int = VOYAGE_BATCH_SIZE,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # Literal key, not VOYAGE_API_KEY_ENV_VAR — see that constant's note.
        resolved_key = api_key or os.environ.get("VOYAGE_API_KEY")
        if not resolved_key:
            raise VoyageEmbeddingError(
                f"{VOYAGE_API_KEY_ENV_VAR} is required to build a VoyageEmbedder "
                f"(the process asked for {EMBEDDER_ENV_VAR}=voyage). Nothing in this "
                "repo calls load_dotenv(), so prefix the command with "
                "`set -a; source .env; set +a`."
            )
        self.dim = output_dimension
        self.model = model
        self.input_type = input_type
        self.batch_size = batch_size
        self._api_key = resolved_key
        self._client = client or httpx.Client(timeout=timeout)
        self._sleep = sleep

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        batch: list[str] = []
        for text in texts:
            batch.append(text)
            if len(batch) == self.batch_size:
                vectors.extend(self._embed_batch(batch))
                batch = []
        if batch:
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _post(self, payload: dict[str, object]) -> httpx.Response:
        """One attempt. Raises ``_VoyageRetryable`` on 429/5xx so the
        ``Retrying`` policy in ``_embed_batch`` can wait and try again, and
        ``VoyageEmbeddingError`` immediately on any other 4xx (a bad key or a
        bad model name is not going to fix itself)."""
        response = self._client.post(
            VOYAGE_API_URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if response.status_code == 429 or response.status_code >= 500:
            raise _VoyageRetryable(response.status_code, response.text)
        if response.status_code >= 400:
            raise VoyageEmbeddingError(
                f"Voyage embeddings request failed: {response.status_code} {response.text}"
            )
        return response

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, object] = {
            "input": texts,
            "model": self.model,
            "output_dimension": self.dim,
        }
        if self.input_type is not None:
            payload["input_type"] = self.input_type

        retrying = Retrying(
            retry=retry_if_exception_type((_VoyageRetryable, httpx.HTTPError)),
            wait=wait_fixed(VOYAGE_RATE_LIMIT_WAIT_SECONDS),
            stop=stop_after_attempt(VOYAGE_MAX_ATTEMPTS),
            sleep=self._sleep,
            reraise=True,
        )
        try:
            response = retrying(self._post, payload)
        except _VoyageRetryable as exc:
            raise VoyageEmbeddingError(
                f"Voyage embeddings request failed after {VOYAGE_MAX_ATTEMPTS} attempts: "
                f"{exc.status_code} {exc.body}"
            ) from None

        data = response.json().get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise VoyageEmbeddingError(
                f"Voyage returned {len(data) if isinstance(data, list) else '?'} embeddings "
                f"for {len(texts)} inputs"
            )
        # Voyage documents that `index` mirrors input order, but the order of
        # the array is not something to take on faith when a mis-ordered
        # reseed would be silently wrong rather than loudly broken.
        ordered = sorted(data, key=lambda item: item["index"])
        vectors = [list(item["embedding"]) for item in ordered]
        for vector in vectors:
            if len(vector) != self.dim:
                raise VoyageEmbeddingError(
                    f"Voyage returned a {len(vector)}-dimension vector; the "
                    f"kb_chunks.embedding column is vector({self.dim})"
                )
        return vectors


# -- default resolution ------------------------------------------------------


# Fallback for an embedder that declares no floor of its own — a caller can
# always inject a custom Embedder implementation, and a Protocol cannot
# require a class attribute without changing its shape (frozen, §1.4). Zero
# means "no floor", i.e. exactly the pre-ADR-010 behaviour, which is the
# only safe default for an embedding space nobody has calibrated.
UNCALIBRATED_MIN_SCORE = 0.0

_EMBEDDERS: dict[str, type] = {"hashing": HashingEmbedder, "voyage": VoyageEmbedder}


def _configured_name() -> str:
    # Literal key, not EMBEDDER_ENV_VAR — see that constant's note.
    name = os.environ.get("KB_EMBEDDER", DEFAULT_EMBEDDER_NAME).strip().lower()
    resolved = name or DEFAULT_EMBEDDER_NAME
    if resolved not in _EMBEDDERS:
        raise ValueError(
            f"{EMBEDDER_ENV_VAR}={name!r} is not a known embedder "
            f"(expected one of {sorted(_EMBEDDERS)})"
        )
    return resolved


def default_embedder(*, input_type: InputType | None = None) -> Embedder:
    """The embedder ``seed_all`` and ``search_kb`` both fall back to.

    ``input_type`` is a hint for backends that distinguish indexing from
    searching (Voyage does; the lexical embedder has no such notion and
    ignores it). Callers pass ``"document"`` when writing vectors and
    ``"query"`` when reading them.
    """
    if _configured_name() == "voyage":
        return VoyageEmbedder(input_type=input_type)
    return HashingEmbedder()


def default_min_score() -> float:
    """The configured embedder's calibrated floor, WITHOUT constructing it.

    ``agent.config.KB_MIN_SCORE`` is resolved at import time, and building a
    ``VoyageEmbedder`` opens an httpx client and demands an API key — a side
    effect no module import should have. Reading the class attribute avoids
    that entirely.
    """
    return float(getattr(_EMBEDDERS[_configured_name()], "min_score", UNCALIBRATED_MIN_SCORE))


def min_score_for(embedder: Embedder) -> float:
    """The calibrated relevance floor for ``embedder``'s score distribution.

    Read off the concrete instance rather than the Protocol: BUILD-PLAN §1.4
    freezes ``Embedder`` at ``dim``/``embed``, so the floor travels as an
    ordinary attribute that ``HashingEmbedder``/``VoyageEmbedder`` both
    declare and an unknown injected implementation simply does not — in
    which case there is no floor, because nobody has calibrated one.
    """
    value = getattr(embedder, "min_score", UNCALIBRATED_MIN_SCORE)
    return float(value)
