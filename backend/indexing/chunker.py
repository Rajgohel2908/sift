"""
indexing/chunker.py — Pure chunking functions for Phase 4 baseline RAG.

Splits active documents and serializes active structured records into clean chunks
ready for embedding. Enforces status='active', duplicate skipping, and metadata tracking.
"""

from typing import List, Dict, Any, Optional
import pandas as pd
from langchain_text_splitters import RecursiveCharacterTextSplitter
from profiler.text_quality import compute_content_hash, check_freshness


def estimate_tokens(text: str) -> int:
    """
    Estimate token count for a piece of text using character length (~4 chars/token).
    Returns at least 1 for non-empty text.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def serialize_record_row(data: Dict[str, Any]) -> str:
    """
    Serialize a structured data row (dict) into a standardized string format:
    "Column: value; Column: value; ..."
    Skips None, NaN, and empty strings.
    Orders keys deterministically with identifiers and department prioritized.
    """
    if not data or not isinstance(data, dict):
        return ""

    def _key_priority(k: str) -> tuple:
        kl = k.lower()
        if "id" in kl:
            return (0, kl)
        if "name" in kl:
            return (1, kl)
        if "department" in kl or "dept" in kl:
            return (2, kl)
        return (3, kl)

    ordered_keys = sorted(data.keys(), key=_key_priority)

    parts = []
    for k in ordered_keys:
        v = data[k]
        if v is None:
            continue
        if isinstance(v, float) and pd.isna(v):
            continue
        v_str = str(v).strip()
        if v_str == "" or v_str.lower() == "nan" or v_str.lower() == "none":
            continue
        parts.append(f"{k}: {v_str}")

    return "; ".join(parts)


def split_document_text(
    content: str,
    chunk_size: int,
    chunk_overlap: int,
) -> List[str]:
    """
    Split unstructured text content using RecursiveCharacterTextSplitter.
    Uses separators ["\\n\\n", "\\n", ". ", " ", ""].
    """
    if not content or not content.strip():
        return []

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    return splitter.split_text(content)


def chunk_active_source_content(
    source_id: int,
    source_name: Optional[str],
    source_type: str,
    source_version: int,
    documents: List[Any],
    records: List[Any],
    chunk_size: int,
    chunk_overlap: int,
) -> List[Dict[str, Any]]:
    """
    Extract and prepare all chunks from active documents and active records
    for a given source and version.

    Rules enforced:
    - Only content with status == 'active' is indexed. Quarantined and excluded content is skipped.
    - Only content matching the specified source_version is indexed.
    - Skip empty or whitespace-only chunks.
    - Skip exact duplicate chunks within this index run (deduplication by content_hash).
    - Unstructured documents: chunked via RecursiveCharacterTextSplitter.
    - Structured records: 1 chunk per row, serialized as "Column: value; Column: value; ...".
    """
    chunks: List[Dict[str, Any]] = []
    seen_hashes = set()
    global_chunk_idx = 0

    # 1. Process active documents
    for doc in documents:
        # Strictly verify active status and version
        status = getattr(doc, "status", "active")
        version_no = getattr(doc, "version_no", 1)
        if status != "active" or version_no != source_version:
            continue

        raw_content = getattr(doc, "content", "") or ""
        text_chunks = split_document_text(
            raw_content,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

        doc_published_at = getattr(doc, "published_at", None)
        freshness_flag = check_freshness(doc_published_at)["flag"]

        for sub_idx, chunk_text in enumerate(text_chunks):
            chunk_text = chunk_text.strip()
            if not chunk_text:
                continue

            content_hash = compute_content_hash(chunk_text)
            if content_hash in seen_hashes:
                # Deduplication: skip exact duplicate chunk within this index run
                continue
            seen_hashes.add(content_hash)

            meta = {
                "source_id": source_id,
                "source_name": source_name,
                "source_type": source_type,
                "document_id": getattr(doc, "id", None),
                "title": getattr(doc, "title", None),
                "url": getattr(doc, "url", None),
                "page_number": getattr(doc, "page_number", None),
                "modality": getattr(doc, "modality", "text"),
                "extraction_confidence": getattr(doc, "extraction_confidence", None),
                "published_at": doc_published_at.isoformat() if doc_published_at else None,
                "freshness": freshness_flag,
                "source_version": source_version,
                "chunk_index": global_chunk_idx,
            }

            chunks.append({
                "source_id": source_id,
                "document_id": getattr(doc, "id", None),
                "record_id": None,
                "chunk_index": global_chunk_idx,
                "content": chunk_text,
                "content_hash": content_hash,
                "token_estimate": estimate_tokens(chunk_text),
                "metadata": meta,
            })
            global_chunk_idx += 1

    # 2. Process active structured records
    for rec in records:
        # Strictly verify active status and version
        status = getattr(rec, "status", "active")
        version_no = getattr(rec, "version_no", 1)
        if status != "active" or version_no != source_version:
            continue

        raw_data = getattr(rec, "data", {}) or {}
        row_text = serialize_record_row(raw_data)
        row_text = row_text.strip()
        if not row_text:
            continue

        content_hash = compute_content_hash(row_text)
        if content_hash in seen_hashes:
            # Deduplication: skip exact duplicate chunk within this index run
            continue
        seen_hashes.add(content_hash)

        # Retrieve row flags from Phase 3 metadata
        rec_meta = getattr(rec, "metadata_", {}) or {}
        if not isinstance(rec_meta, dict):
            rec_meta = {}
        flags = rec_meta.get("flags", rec_meta)

        meta = {
            "source_id": source_id,
            "record_id": getattr(rec, "id", None),
            "row_index": getattr(rec, "row_index", None),
            "source_version": source_version,
            "flags": flags,
        }

        chunks.append({
            "source_id": source_id,
            "document_id": None,
            "record_id": getattr(rec, "id", None),
            "chunk_index": global_chunk_idx,
            "content": row_text,
            "content_hash": content_hash,
            "token_estimate": estimate_tokens(row_text),
            "metadata": meta,
        })
        global_chunk_idx += 1

    return chunks
