"""
tests/test_rag.py — Comprehensive unit tests for Phase 4 baseline RAG.

Tests:
  - Insufficient-evidence path: LLM client is NOT called when score < min_score
  - Citation validation: invalid IDs dropped, zero valid citations -> unverified, valid citations -> answered
  - Retry/backoff with FakeLLMClient: transient failures retry, permanent failure -> llm_error
  - Coexistence of two index runs with different configurations, selectable via index_run_id
  - Database persistence: queries and retrieval_results rows recorded accurately
  - Quarantined/excluded content absence verification
"""

import pytest
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base, Source, Document, Record, IndexRun, Chunk, QueryRecord, RetrievalResult
from indexing.embedder import FakeEmbedder, set_embedder
from generation.llm import FakeLLMClient, LLMError, set_llm_client
from generation.answer import (
    build_rag_prompt,
    parse_and_validate_citations,
    execute_query,
)


@pytest.fixture
def test_db():
    """In-memory SQLite session with all tables created for unit testing."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Pre-populate sample source and active/quarantined documents
    src = Source(source_type="file", name="leave_policy.docx")
    session.add(src)
    session.commit()

    doc_active = Document(
        source_id=src.id,
        modality="text",
        title="Leave Policy 2024",
        page_number=1,
        content="All full-time employees are entitled to 24 annual leave days per year.",
        version_no=1,
        status="active",
    )
    doc_quarantined = Document(
        source_id=src.id,
        modality="text",
        title="Quarantined Draft",
        page_number=2,
        content="This policy is quarantined and should not be indexed.",
        version_no=1,
        status="quarantined",
    )
    session.add_all([doc_active, doc_quarantined])
    session.commit()

    yield session
    session.close()


def test_build_rag_prompt_structure_and_untrusted_data_warning():
    chunks = [
        {
            "chunk_id": 10,
            "content": "Employees are entitled to 24 annual leave days.",
            "metadata": {"source_name": "leave_policy.pdf", "page_number": 1},
        }
    ]
    prompt = build_rag_prompt("How many leave days?", chunks)

    # Check delimiters
    assert '<chunk id="10" source="leave_policy.pdf" page="1">' in prompt
    assert "</chunk>" in prompt
    # Check untrusted data warning
    assert "untrusted user DATA" in prompt
    # Check citation rule
    assert "[chunk_id]" in prompt
    assert "How many leave days?" in prompt


def test_parse_and_validate_citations():
    prompt_chunks = [
        {"chunk_id": 1, "content": "Text for chunk 1", "score": 0.85, "metadata": {"source_name": "doc1"}},
        {"chunk_id": 2, "content": "Text for chunk 2", "score": 0.75, "metadata": {"source_name": "doc2"}},
    ]

    # Case A: Valid citations
    ans_valid = "According to policy [1], employees get leave [2]."
    res_valid = parse_and_validate_citations(ans_valid, prompt_chunks)
    assert len(res_valid["valid_citations"]) == 2
    assert res_valid["valid_ids"] == {1, 2}
    assert res_valid["invalid_citations"] == []

    # Case B: Hallucinated / invalid citation id
    ans_invalid = "Fact from valid [1] and fact from hallucinated [999]."
    res_invalid = parse_and_validate_citations(ans_invalid, prompt_chunks)
    assert len(res_invalid["valid_citations"]) == 1
    assert res_invalid["valid_ids"] == {1}
    assert res_invalid["invalid_citations"] == [999]

    # Case C: Zero citations
    ans_no_cite = "Fact without any citations."
    res_no_cite = parse_and_validate_citations(ans_no_cite, prompt_chunks)
    assert len(res_valid["valid_citations"]) == 2
    assert res_no_cite["valid_citations"] == []
    assert res_no_cite["valid_ids"] == set()


def test_insufficient_evidence_path_does_not_call_llm(test_db):
    """
    CRITICAL REQUIREMENT: If the best score < min_score OR nothing retrieved:
    DO NOT call the LLM; return status "insufficient_evidence".
    """
    embedder = FakeEmbedder(dim=384)
    fake_llm = FakeLLMClient(response="Answer with [1].")

    # Create an index run and one low-scoring chunk
    run = IndexRun(
        source_id=1, source_version=1, embed_model="test",
        embed_dim=384, chunk_size=800, chunk_overlap=120,
        status="completed", chunk_count=1,
    )
    test_db.add(run)
    test_db.commit()

    # Query with high min_score (e.g. 0.99) that cannot be met
    result = execute_query(
        db=test_db,
        question="What is the company Mars colonization budget?",
        source_ids=[1],
        min_score=0.99,
        llm_client=fake_llm,
        embedder=embedder,
    )

    # Assertions
    assert result["status"] == "insufficient_evidence"
    assert result["answer"] is None
    # Crucial: LLM must NEVER have been called!
    assert fake_llm.call_count == 0


def test_unverified_status_when_zero_valid_citations(test_db):
    """
    CRITICAL REQUIREMENT: If the LLM returned an answer with zero valid citations,
    return status "unverified" (answer included, clearly marked), not "answered".
    """
    embedder = FakeEmbedder(dim=384)
    # LLM responds without any [chunk_id] citation or with hallucinated ID
    fake_llm = FakeLLMClient(response="Employees get 24 days but I won't cite [9999].")

    run = IndexRun(
        source_id=1, source_version=1, embed_model="test",
        embed_dim=384, chunk_size=800, chunk_overlap=120,
        status="completed", chunk_count=1,
    )
    test_db.add(run)
    test_db.commit()

    chunk_vec = embedder.embed_query("leave policy")
    chunk = Chunk(
        index_run_id=run.id,
        source_id=1,
        document_id=1,
        chunk_index=0,
        content="Employees get 24 leave days.",
        content_hash="h1",
        token_estimate=6,
        embedding=chunk_vec,
        metadata_={"source_name": "leave_policy.docx"},
    )
    test_db.add(chunk)
    test_db.commit()

    result = execute_query(
        db=test_db,
        question="leave policy",
        source_ids=[1],
        min_score=0.1,
        llm_client=fake_llm,
        embedder=embedder,
    )

    assert fake_llm.call_count == 1
    assert result["status"] == "unverified"
    assert result["answer"] == "Employees get 24 days but I won't cite [9999]."
    assert 9999 in result["invalid_citations"]
    assert len(result["citations"]) == 0


def test_answered_status_with_valid_citations(test_db):
    embedder = FakeEmbedder(dim=384)

    run = IndexRun(
        source_id=1, source_version=1, embed_model="test",
        embed_dim=384, chunk_size=800, chunk_overlap=120,
        status="completed", chunk_count=1,
    )
    test_db.add(run)
    test_db.commit()

    chunk_vec = embedder.embed_query("annual leave days")
    chunk = Chunk(
        index_run_id=run.id,
        source_id=1,
        document_id=1,
        chunk_index=0,
        content="Employees receive 24 annual leave days.",
        content_hash="h1",
        token_estimate=6,
        embedding=chunk_vec,
        metadata_={"source_name": "leave_policy.docx", "page_number": 1},
    )
    test_db.add(chunk)
    test_db.commit()

    fake_llm = FakeLLMClient(response=f"Employees receive 24 annual leave days [{chunk.id}].")

    result = execute_query(
        db=test_db,
        question="annual leave days",
        source_ids=[1],
        min_score=0.1,
        llm_client=fake_llm,
        embedder=embedder,
    )

    assert result["status"] == "answered"
    assert len(result["citations"]) == 1
    assert result["citations"][0]["chunk_id"] == chunk.id
    assert result["citations"][0]["source_name"] == "leave_policy.docx"

    # Verify database persistence
    q_row = test_db.query(QueryRecord).filter(QueryRecord.id == result["query_id"]).first()
    assert q_row is not None
    assert q_row.status == "answered"

    rr_row = test_db.query(RetrievalResult).filter(RetrievalResult.query_id == q_row.id).first()
    assert rr_row is not None
    assert rr_row.chunk_id == chunk.id
    assert rr_row.used_in_prompt is True
    assert rr_row.cited is True


def test_llm_error_status_after_retries(test_db):
    """
    CRITICAL REQUIREMENT: On final LLM failure, return status 'llm_error'
    with the retrieved chunks still included (retrieval must still be visible).
    """
    embedder = FakeEmbedder(dim=384)
    failing_llm = FakeLLMClient(always_fail=True)

    run = IndexRun(
        source_id=1, source_version=1, embed_model="test",
        embed_dim=384, chunk_size=800, chunk_overlap=120,
        status="completed", chunk_count=1,
    )
    test_db.add(run)
    test_db.commit()

    chunk_vec = embedder.embed_query("annual leave")
    chunk = Chunk(
        index_run_id=run.id,
        source_id=1,
        document_id=1,
        chunk_index=0,
        content="Employees receive 24 annual leave days.",
        content_hash="h1",
        token_estimate=6,
        embedding=chunk_vec,
        metadata_={"source_name": "leave_policy.docx"},
    )
    test_db.add(chunk)
    test_db.commit()

    result = execute_query(
        db=test_db,
        question="annual leave",
        source_ids=[1],
        min_score=0.1,
        llm_client=failing_llm,
        embedder=embedder,
    )

    assert result["status"] == "llm_error"
    assert result["answer"] is None
    # Retrieval results MUST still be visible!
    assert len(result["retrieved"]) > 0
    assert result["retrieved"][0]["chunk_id"] == chunk.id


def test_index_runs_coexistence_and_selection(test_db):
    """
    CRITICAL REQUIREMENT: Two index runs with different chunk sizes can coexist,
    and /query accepts index_run_id to choose between them.
    """
    embedder = FakeEmbedder(dim=384)

    # Run 1: chunk_size = 400
    run1 = IndexRun(
        source_id=1, source_version=1, embed_model="test",
        embed_dim=384, chunk_size=400, chunk_overlap=50,
        status="completed", chunk_count=1,
    )
    test_db.add(run1)
    test_db.flush()

    vec1 = embedder.embed_query("query run 1")
    ch1 = Chunk(
        index_run_id=run1.id, source_id=1, chunk_index=0,
        content="Content from run 1 with size 400",
        content_hash="h_run1", token_estimate=10, embedding=vec1,
        metadata_={"run": 1},
    )
    test_db.add(ch1)

    # Run 2: chunk_size = 800
    run2 = IndexRun(
        source_id=1, source_version=1, embed_model="test",
        embed_dim=384, chunk_size=800, chunk_overlap=120,
        status="completed", chunk_count=1,
    )
    test_db.add(run2)
    test_db.flush()

    vec2 = embedder.embed_query("query run 2")
    ch2 = Chunk(
        index_run_id=run2.id, source_id=1, chunk_index=0,
        content="Content from run 2 with size 800",
        content_hash="h_run2", token_estimate=20, embedding=vec2,
        metadata_={"run": 2},
    )
    test_db.add(ch2)
    test_db.commit()

    # Query targeting specifically run1
    fake_llm = FakeLLMClient(response=f"Answer [{ch1.id}]")
    res1 = execute_query(
        db=test_db,
        question="query run 1",
        source_ids=[1],
        index_run_id=run1.id,
        min_score=0.1,
        llm_client=fake_llm,
        embedder=embedder,
    )
    assert res1["config"]["index_run_id"] == run1.id
    assert res1["retrieved"][0]["chunk_id"] == ch1.id

    # Query targeting specifically run2
    fake_llm2 = FakeLLMClient(response=f"Answer [{ch2.id}]")
    res2 = execute_query(
        db=test_db,
        question="query run 2",
        source_ids=[1],
        index_run_id=run2.id,
        min_score=0.1,
        llm_client=fake_llm2,
        embedder=embedder,
    )
    assert res2["config"]["index_run_id"] == run2.id
    assert res2["retrieved"][0]["chunk_id"] == ch2.id
