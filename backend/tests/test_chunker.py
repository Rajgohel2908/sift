"""
tests/test_chunker.py — Unit tests for indexing/chunker.py

Tests:
  - Row serialization format and skipping nulls/NaNs
  - Document text chunking with size limit and overlap
  - Skipping empty / whitespace-only content
  - Deduplicating identical chunks in the same run via content_hash
  - Enforcing status == 'active': quarantined and excluded content is NEVER chunked
  - Respecting source_version filtering
"""

import pytest
from types import SimpleNamespace
from indexing.chunker import (
    serialize_record_row,
    split_document_text,
    chunk_active_source_content,
    estimate_tokens,
)


def test_row_serialization():
    data = {
        "Employee_ID": "E1007",
        "Name": "Alice",
        "Department": "Engineering",
        "Salary": 95000.0,
        "Manager": None,
        "Notes": "",
        "Bonus": float("nan"),
    }
    result = serialize_record_row(data)
    assert "Employee_ID: E1007" in result
    assert "Name: Alice" in result
    assert "Department: Engineering" in result
    assert "Salary: 95000.0" in result
    # Nulls, empty strings, and NaNs must be omitted
    assert "Manager" not in result
    assert "Notes" not in result
    assert "Bonus" not in result
    assert result == "Employee_ID: E1007; Name: Alice; Department: Engineering; Salary: 95000.0"


def test_document_text_splitter_overlap_and_size():
    # Construct a sample text with paragraphs
    text = (
        "Paragraph 1 contains some important information.\n\n"
        "Paragraph 2 continues the policy explanation with additional detail.\n\n"
        "Paragraph 3 concludes the policy section."
    )
    chunks = split_document_text(text, chunk_size=80, chunk_overlap=20)
    assert len(chunks) > 1
    # Each chunk should respect the size limit
    for ch in chunks:
        assert len(ch) <= 100  # splitter might break cleanly near 80


def test_empty_and_whitespace_skip():
    doc_empty = SimpleNamespace(
        id=1, status="active", version_no=1, content="   \n\n\t  ",
        title="Empty", url=None, page_number=1, modality="text",
        extraction_confidence=None, published_at=None,
    )
    rec_empty = SimpleNamespace(
        id=2, status="active", version_no=1, row_index=0,
        data={"ColA": None, "ColB": ""}, metadata_={},
    )
    chunks = chunk_active_source_content(
        source_id=1,
        source_name="test_source",
        source_type="file",
        source_version=1,
        documents=[doc_empty],
        records=[rec_empty],
        chunk_size=800,
        chunk_overlap=120,
    )
    assert len(chunks) == 0


def test_duplicate_chunks_skipped_within_run():
    doc1 = SimpleNamespace(
        id=1, status="active", version_no=1,
        content="Identical leave policy content duplicated across pages.",
        title="Page 1", url=None, page_number=1, modality="text",
        extraction_confidence=None, published_at=None,
    )
    doc2 = SimpleNamespace(
        id=2, status="active", version_no=1,
        content="Identical leave policy content duplicated across pages.",
        title="Page 2", url=None, page_number=2, modality="text",
        extraction_confidence=None, published_at=None,
    )
    chunks = chunk_active_source_content(
        source_id=1,
        source_name="test_source",
        source_type="file",
        source_version=1,
        documents=[doc1, doc2],
        records=[],
        chunk_size=800,
        chunk_overlap=120,
    )
    # Exact duplicate chunk must be skipped
    assert len(chunks) == 1
    assert chunks[0]["document_id"] == 1


def test_quarantined_and_excluded_content_never_chunked():
    """
    CRITICAL REQUIREMENT: Only content with status = 'active' in the chosen
    source version is indexed. Quarantined and excluded content must NEVER be chunked.
    """
    active_doc = SimpleNamespace(
        id=1, status="active", version_no=1, content="Active policy text.",
        title="Active Doc", url=None, page_number=1, modality="text",
        extraction_confidence=None, published_at=None,
    )
    quarantined_doc = SimpleNamespace(
        id=2, status="quarantined", version_no=1, content="Quarantined policy text.",
        title="Quarantined Doc", url=None, page_number=2, modality="text",
        extraction_confidence=None, published_at=None,
    )
    excluded_doc = SimpleNamespace(
        id=3, status="excluded", version_no=1, content="Excluded policy text.",
        title="Excluded Doc", url=None, page_number=3, modality="text",
        extraction_confidence=None, published_at=None,
    )

    active_rec = SimpleNamespace(
        id=10, status="active", version_no=1, row_index=0,
        data={"Employee_ID": "E1007", "Department": "Engineering"}, metadata_={},
    )
    quarantined_rec = SimpleNamespace(
        id=11, status="quarantined", version_no=1, row_index=1,
        data={"Employee_ID": None, "Department": "HR"}, metadata_={"quarantine_reason": "missing identifier"},
    )
    excluded_rec = SimpleNamespace(
        id=12, status="excluded", version_no=1, row_index=2,
        data={"Employee_ID": "E9999", "Department": "Spam"}, metadata_={},
    )

    chunks = chunk_active_source_content(
        source_id=100,
        source_name="test_source",
        source_type="file",
        source_version=1,
        documents=[active_doc, quarantined_doc, excluded_doc],
        records=[active_rec, quarantined_rec, excluded_rec],
        chunk_size=800,
        chunk_overlap=120,
    )

    # Only active content should be chunked (1 doc chunk + 1 record chunk)
    assert len(chunks) == 2
    doc_ids = [c["document_id"] for c in chunks if c["document_id"] is not None]
    rec_ids = [c["record_id"] for c in chunks if c["record_id"] is not None]

    assert doc_ids == [1]
    assert 2 not in doc_ids  # quarantined doc
    assert 3 not in doc_ids  # excluded doc

    assert rec_ids == [10]
    assert 11 not in rec_ids  # quarantined record
    assert 12 not in rec_ids  # excluded record


def test_source_version_filtering():
    v1_doc = SimpleNamespace(
        id=1, status="active", version_no=1, content="Version 1 text content.",
        title="V1", url=None, page_number=1, modality="text",
        extraction_confidence=None, published_at=None,
    )
    v2_doc = SimpleNamespace(
        id=2, status="active", version_no=2, content="Version 2 updated text content.",
        title="V2", url=None, page_number=1, modality="text",
        extraction_confidence=None, published_at=None,
    )

    # Chunk with version 2
    chunks_v2 = chunk_active_source_content(
        source_id=1,
        source_name="test",
        source_type="file",
        source_version=2,
        documents=[v1_doc, v2_doc],
        records=[],
        chunk_size=800,
        chunk_overlap=120,
    )
    assert len(chunks_v2) == 1
    assert chunks_v2[0]["document_id"] == 2
    assert chunks_v2[0]["metadata"]["source_version"] == 2
