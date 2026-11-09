"""Embedding providers.

The default is a local model (``bge-small``), so record text never leaves
the system boundary to be embedded. ``HashingEmbedder`` is deterministic and
dependency-free. Tests, replay mode and CI use it, so they need no model
download. Both produce 384-dimensional, L2-normalized vectors and share the
same pgvector column.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import os
import re
from typing import Protocol

DIM = 384


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Feature-hashing bag of unigrams and bigrams. Lexical only, but deterministic."""

    name = "hashing-384"
    dim = DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = re.findall(r"[a-z0-9]+", text.lower())
        feats = tokens + [f"{a}_{b}" for a, b in itertools.pairwise(tokens)]
        for feat in feats:
            h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "big")
            vec[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class SentenceTransformerEmbedder:  # pragma: no cover - needs optional model download
    """Local open embedding model via sentence-transformers (``pip install .[embeddings]``)."""

    dim = DIM

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5") -> None:
        from sentence_transformers import SentenceTransformer

        self.name = model
        self._model = SentenceTransformer(model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [list(map(float, v)) for v in vectors]


def get_embedder(kind: str | None = None) -> Embedder:
    kind = kind or os.environ.get("MEDCHRON_EMBEDDER", "hashing")
    if kind == "hashing":
        return HashingEmbedder()
    if kind == "bge-small":
        return SentenceTransformerEmbedder()
    raise ValueError(f"unknown embedder {kind!r} (use 'hashing' or 'bge-small')")
