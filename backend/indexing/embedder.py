"""
indexing/embedder.py — Embedder interface and SentenceTransformer implementation.

Provides a lazy-loaded singleton for CPU sentence embeddings with L2 normalization,
batching in chunks of 32, and an abstract interface for swappability and testing.
"""

from abc import ABC, abstractmethod
from typing import List, Optional
import hashlib
import numpy as np

import config


class Embedder(ABC):
    """Abstract Embedder interface for generating vector representations."""

    @abstractmethod
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Embed a batch of text documents."""
        pass

    @abstractmethod
    def embed_query(self, text: str) -> List[float]:
        """Embed a single search query."""
        pass

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Embedding vector dimension."""
        pass


class SentenceTransformerEmbedder(Embedder):
    """
    SentenceTransformer implementation using CPU by default.
    Normalizes embeddings to unit length (for cosine similarity) and batches by 32.
    """

    def __init__(self, model_name: Optional[str] = None, device: str = "cpu"):
        self.model_name = model_name or config.EMBED_MODEL
        self.device = device
        self._model = None
        self._dim = None

    def _ensure_loaded(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name, device=self.device)
            if hasattr(self._model, "get_embedding_dimension"):
                self._dim = self._model.get_embedding_dimension()
            else:
                self._dim = self._model.get_sentence_embedding_dimension()
            # Fail fast if dimension does not match database column
            config.validate_embed_dim(self._dim)

    @property
    def dimension(self) -> int:
        self._ensure_loaded()
        return self._dim

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        self._ensure_loaded()
        embeddings = self._model.encode(
            texts,
            batch_size=32,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return embeddings.tolist()

    def embed_query(self, text: str) -> List[float]:
        self._ensure_loaded()
        emb = self._model.encode(
            [text],
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )[0]
        return emb.tolist()


class FakeEmbedder(Embedder):
    """
    Deterministic test embedder for unit tests (requires no downloads or GPU/CPU model weights).
    Generates deterministic normalized vectors of the requested dimension based on text hash.
    """

    def __init__(self, dim: int = 384):
        self._dim = dim

    @property
    def dimension(self) -> int:
        return self._dim

    def _hash_to_vec(self, text: str) -> List[float]:
        # Generate reproducible pseudo-random vector from MD5 hash of text
        h = hashlib.md5(text.encode("utf-8")).digest()
        # Seed a local random generator with hash bytes
        seed = int.from_bytes(h[:4], "little")
        rng = np.random.RandomState(seed)
        vec = rng.randn(self._dim).astype(np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.tolist()

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._hash_to_vec(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._hash_to_vec(text)


# Lazy singleton instance
_embedder_instance: Optional[Embedder] = None


def get_embedder(model_name: Optional[str] = None) -> Embedder:
    """Return the global Embedder singleton (loads SentenceTransformers on first call)."""
    global _embedder_instance
    if _embedder_instance is None:
        _embedder_instance = SentenceTransformerEmbedder(model_name=model_name)
    return _embedder_instance


def set_embedder(embedder: Embedder) -> None:
    """Override the global embedder (used in tests to inject FakeEmbedder)."""
    global _embedder_instance
    _embedder_instance = embedder
