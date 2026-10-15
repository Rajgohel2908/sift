"""
tests/test_rag_integration.py — Slow integration test for Phase 4 baseline RAG.

Marked slow and skipped by default unless environment variable RUN_SLOW=1 is set.
Uses the real SentenceTransformer embedding model on CPU with FakeLLMClient.
"""

import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base, Source, Document, IndexRun, Chunk
from indexing.embedder import SentenceTransformerEmbedder
from generation.llm import FakeLLMClient
from generation.answer import execute_query


@pytest.mark.skipif(
    not os.getenv("RUN_SLOW"),
    reason="Slow integration test using real embedding model; enable with RUN_SLOW=1",
)
def test_real_embedding_model_integration():
    """
    Integration test running the actual sentence-transformers model (CPU)
    against an in-memory database with real cosine similarity calculation.
    """
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    embedder = SentenceTransformerEmbedder()
    assert embedder.dimension == 384

    # Create source and document
    src = Source(source_type="file", name="hr_policy.docx")
    db.add(src)
    db.commit()

    run = IndexRun(
        source_id=src.id,
        source_version=1,
        embed_model="sentence-transformers/all-MiniLM-L6-v2",
        embed_dim=384,
        chunk_size=800,
        chunk_overlap=120,
        chunk_count=2,
        status="completed",
    )
    db.add(run)
    db.commit()

    text_leave = "Employees receive 24 annual leave days per calendar year."
    text_unrelated = "The company data center uses 100% renewable wind energy."

    embeddings = embedder.embed_documents([text_leave, text_unrelated])

    ch_leave = Chunk(
        index_run_id=run.id,
        source_id=src.id,
        chunk_index=0,
        content=text_leave,
        content_hash="hash_leave",
        token_estimate=10,
        embedding=embeddings[0],
        metadata_={"source_name": "hr_policy.docx"},
    )
    ch_unrelated = Chunk(
        index_run_id=run.id,
        source_id=src.id,
        chunk_index=1,
        content=text_unrelated,
        content_hash="hash_unrelated",
        token_estimate=10,
        embedding=embeddings[1],
        metadata_={"source_name": "hr_policy.docx"},
    )
    db.add_all([ch_leave, ch_unrelated])
    db.commit()

    fake_llm = FakeLLMClient(response=f"According to company policy, staff get 24 days [{ch_leave.id}].")

    # Ask semantically related question
    result = execute_query(
        db=db,
        question="How many days of annual leave do employees get?",
        source_ids=[src.id],
        min_score=0.3,
        llm_client=fake_llm,
        embedder=embedder,
    )

    assert result["status"] == "answered"
    assert result["retrieved"][0]["chunk_id"] == ch_leave.id
    assert result["retrieved"][0]["score"] > 0.5  # Semantic similarity should be high
    assert len(result["citations"]) == 1
    assert result["citations"][0]["chunk_id"] == ch_leave.id

    db.close()
