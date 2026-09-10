"""Chunk embeddings.

ADR-0002 D1: ``BAAI/bge-small-en-v1.5`` run locally on onnxruntime via fastembed. No API
call, no key, no GPU — which is what keeps the Phase 4 evaluation harness runnable in CI
on every push rather than only when someone is willing to spend money.

The provider is an interface with two implementations: the real one, and a deterministic
fake used by tests so the suite never loads a model or touches the network.
"""

from __future__ import annotations

import hashlib
import math
import threading
from typing import Protocol

from gri.logging import get_logger
from gri.models import EMBEDDING_DIM, EMBEDDING_MODEL

log = get_logger(__name__)


class EmbeddingProvider(Protocol):
    """Turns text into vectors."""

    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch. Output order matches input order."""
        ...


class FastEmbedProvider:
    """The real provider. Loads the ONNX model once, lazily, and reuses it.

    Model loading is deferred to first use rather than done at import, so that importing
    ``gri.processing`` in a test or a migration does not pay for it. The lock makes the
    lazy init safe if two worker threads embed at once.
    """

    def __init__(self, model_name: str = EMBEDDING_MODEL) -> None:
        self._model_name = model_name
        self._model: object | None = None
        self._lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIM

    def _ensure_model(self) -> object:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from fastembed import TextEmbedding

                    log.info("embedding_model_loading", model=self._model_name)
                    self._model = TextEmbedding(model_name=self._model_name)
                    log.info("embedding_model_loaded", model=self._model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure_model()
        vectors = [list(map(float, v)) for v in model.embed(texts)]  # type: ignore[attr-defined]

        for vector in vectors:
            if len(vector) != EMBEDDING_DIM:
                # A model swap that silently changed dimension would corrupt the column
                # rather than fail, so this is checked rather than trusted.
                raise ValueError(
                    f"{self._model_name} returned {len(vector)} dims, expected {EMBEDDING_DIM}"
                )
        return vectors


class DeterministicFakeProvider:
    """A hash-based stand-in with the same shape as the real provider.

    Not an approximation of semantics — it is a fixture. Identical text embeds
    identically and different text embeds differently, which is all the storage,
    clustering-plumbing, and API tests need. Tests that assert something about *meaning*
    seed the vectors explicitly instead of relying on this.
    """

    def __init__(self, model_name: str = "fake-deterministic", dimension: int = EMBEDDING_DIM):
        self._model_name = model_name
        self._dimension = dimension

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Stretch the digest deterministically to the required width.
        raw: list[float] = []
        counter = 0
        while len(raw) < self._dimension:
            block = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
            raw.extend((b - 127.5) / 127.5 for b in block)
            counter += 1
        return normalise_vector(raw[: self._dimension])


def normalise_vector(vector: list[float]) -> list[float]:
    """Scale to unit length so that cosine similarity is a plain dot product."""
    magnitude = math.sqrt(sum(v * v for v in vector))
    if magnitude == 0:
        return list(vector)
    return [v / magnitude for v in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Cosine similarity in [-1, 1]. Returns 0.0 if either vector is degenerate."""
    if len(left) != len(right):
        raise ValueError(f"dimension mismatch: {len(left)} vs {len(right)}")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_mag = math.sqrt(sum(a * a for a in left))
    right_mag = math.sqrt(sum(b * b for b in right))
    if left_mag == 0 or right_mag == 0:
        return 0.0
    return dot / (left_mag * right_mag)


def centroid(vectors: list[list[float]]) -> list[float]:
    """Mean of a document's chunk vectors, normalised to unit length.

    A document-level vector rather than a chunk-level one is what clustering compares
    (ADR-0002 D5): two notices about the same incident should match on the whole notice,
    not because they happen to share one boilerplate sentence.
    """
    if not vectors:
        return []
    width = len(vectors[0])
    if any(len(v) != width for v in vectors):
        raise ValueError("cannot average vectors of differing dimension")
    summed = [sum(v[i] for v in vectors) for i in range(width)]
    return normalise_vector([s / len(vectors) for s in summed])


_default_provider: EmbeddingProvider | None = None


def get_provider() -> EmbeddingProvider:
    """Process-wide provider, so the model is loaded at most once."""
    global _default_provider
    if _default_provider is None:
        _default_provider = FastEmbedProvider()
    return _default_provider


def set_provider(provider: EmbeddingProvider | None) -> None:
    """Override the provider. Test-support only."""
    global _default_provider
    _default_provider = provider
