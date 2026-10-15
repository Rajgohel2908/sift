"""
tests/test_embedder.py — Unit tests for indexing/embedder.py and dimension validation.

Tests:
  - Embedder interface with FakeEmbedder (no model download in unit tests)
  - Deterministic embeddings and vector normalization
  - Dimension mismatch fail-fast logic
"""

import pytest
import numpy as np
import config
from indexing.embedder import FakeEmbedder, set_embedder, get_embedder


def test_fake_embedder_deterministic_and_normalized():
    embedder = FakeEmbedder(dim=384)
    v1 = embedder.embed_query("Annual leave policy 2024")
    v2 = embedder.embed_query("Annual leave policy 2024")
    v3 = embedder.embed_query("Completely different topic")

    # Determinism
    assert len(v1) == 384
    assert v1 == v2
    assert v1 != v3

    # Normalized to unit length (L2 norm ≈ 1.0)
    norm = np.linalg.norm(np.array(v1))
    assert pytest.approx(norm, 1e-5) == 1.0


def test_fake_embedder_batch_documents():
    embedder = FakeEmbedder(dim=384)
    texts = ["Doc 1", "Doc 2", "Doc 3"]
    vecs = embedder.embed_documents(texts)
    assert len(vecs) == 3
    for v in vecs:
        assert len(v) == 384
        assert pytest.approx(np.linalg.norm(v), 1e-5) == 1.0


def test_dimension_mismatch_fail_fast():
    """
    CRITICAL REQUIREMENT: If EMBED_MODEL's dimension does not match
    the chunks.embedding column dimension (384), fail fast with a clear
    message explaining a new migration is needed. Do not silently truncate or pad.
    """
    # Matching dimension should succeed
    config.validate_embed_dim(384)

    # Mismatched dimension (e.g. 512 or 768 or 1536) must fail fast with ValueError
    with pytest.raises(ValueError) as excinfo:
        config.validate_embed_dim(768)

    err_msg = str(excinfo.value).lower()
    assert "migration" in err_msg
    assert "dimension" in err_msg
    assert "768" in err_msg
    assert "384" in err_msg
