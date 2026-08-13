"""Embedding layer.

``Embedder`` is a narrow Protocol so an OpenAI-backed implementation can drop
in later without touching any caller. The default implementation is a
deterministic, fully offline, lexical embedder: this environment has no
``OPENAI_API_KEY``, and retrieval needs to be reproducible byte-for-byte in
CI and in tests without any network call.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sklearn.feature_extraction.text import HashingVectorizer

# Fixed width so it maps directly onto the `kb_chunks.embedding vector(N)`
# column — the whole reason to prefer HashingVectorizer over a fitted
# vectorizer (e.g. TfidfVectorizer), whose vocabulary size varies with the
# corpus.
EMBEDDING_DIM = 1024


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
