"""
retrieval/dense.py — Plain dense cosine similarity retrieval using pgvector.

Retrieves top-k chunks for a query from active index runs using cosine distance.
Falls back to numpy cosine calculation when running tests on SQLite.
"""

from typing import List, Dict, Any, Optional, Tuple
import numpy as np
from sqlalchemy.orm import Session
from sqlalchemy import text

from db import Chunk, IndexRun
from indexing.embedder import Embedder, get_embedder


def resolve_index_run_ids(
    db: Session,
    source_ids: List[int],
    index_run_id: Optional[int] = None,
) -> List[int]:
    """
    Resolve which index runs to retrieve chunks from.
    If index_run_id is explicitly specified, uses that single run.
    Otherwise, selects the latest completed index run for each source in source_ids.
    """
    if index_run_id is not None:
        run = db.query(IndexRun).filter(IndexRun.id == index_run_id).first()
        if not run:
            return []
        return [run.id]

    resolved = []
    for sid in source_ids:
        latest = (
            db.query(IndexRun)
            .filter(IndexRun.source_id == sid, IndexRun.status == "completed")
            .order_by(IndexRun.id.desc())
            .first()
        )
        if latest:
            resolved.append(latest.id)
    return resolved


def retrieve_dense(
    db: Session,
    question: str,
    source_ids: List[int],
    index_run_id: Optional[int] = None,
    top_k: int = 5,
    embedder: Optional[Embedder] = None,
) -> Tuple[List[Dict[str, Any]], List[int], List[float]]:
    """
    Execute dense vector cosine similarity search against chunks.

    Returns:
        (retrieved_chunks, resolved_index_run_ids, query_embedding)
        Each retrieved chunk contains:
          - chunk_id: int
          - rank: int (1-based)
          - score: float (1.0 - cosine_distance)
          - content: str
          - metadata: dict
          - source_id: int
          - document_id: int | None
          - record_id: int | None
          - index_run_id: int
    """
    if not source_ids and index_run_id is None:
        return [], [], []

    # 1. Resolve index runs to search within
    run_ids = resolve_index_run_ids(db, source_ids=source_ids, index_run_id=index_run_id)
    if not run_ids:
        return [], [], []

    # 2. Embed the query
    embedder = embedder or get_embedder()
    query_vec = embedder.embed_query(question)

    bind_engine = db.get_bind()
    is_postgres = (bind_engine.dialect.name == "postgresql")

    results = []

    if is_postgres:
        # Format vector as string literal for pgvector: '[0.1,0.2,...]'
        vec_literal = "[" + ",".join(str(x) for x in query_vec) + "]"

        # Plain pgvector cosine distance: score = 1.0 - (embedding <=> :vec)
        # We query top_k matching chunks in resolved index runs
        sql = text("""
            SELECT 
                id, index_run_id, source_id, document_id, record_id,
                chunk_index, content, metadata,
                1.0 - (embedding <=> CAST(:vec_literal AS vector)) AS score
            FROM chunks
            WHERE index_run_id = ANY(:run_ids)
            ORDER BY embedding <=> CAST(:vec_literal AS vector) ASC
            LIMIT :top_k;
        """)

        rows = db.execute(
            sql,
            {
                "vec_literal": vec_literal,
                "run_ids": run_ids,
                "top_k": top_k,
            },
        ).fetchall()

        for rank, r in enumerate(rows, start=1):
            meta = r.metadata if isinstance(r.metadata, dict) else {}
            results.append({
                "chunk_id": r.id,
                "rank": rank,
                "score": round(float(r.score), 4),
                "content": r.content,
                "metadata": meta,
                "source_id": r.source_id,
                "document_id": r.document_id,
                "record_id": r.record_id,
                "index_run_id": r.index_run_id,
            })
    else:
        # SQLite / in-memory fallback for testing
        all_chunks = (
            db.query(Chunk)
            .filter(Chunk.index_run_id.in_(run_ids))
            .all()
        )
        scored = []
        q_norm = np.linalg.norm(query_vec)

        for ch in all_chunks:
            if ch.embedding is None:
                continue
            emb = np.array(ch.embedding, dtype=np.float32)
            c_norm = np.linalg.norm(emb)
            if q_norm > 0 and c_norm > 0:
                sim = float(np.dot(query_vec, emb) / (q_norm * c_norm))
            else:
                sim = 0.0
            scored.append((sim, ch))

        # Sort descending by similarity
        scored.sort(key=lambda x: x[0], reverse=True)
        top_matches = scored[:top_k]

        for rank, (score, ch) in enumerate(top_matches, start=1):
            meta = ch.metadata_ if isinstance(ch.metadata_, dict) else {}
            results.append({
                "chunk_id": ch.id,
                "rank": rank,
                "score": round(score, 4),
                "content": ch.content,
                "metadata": meta,
                "source_id": ch.source_id,
                "document_id": ch.document_id,
                "record_id": ch.record_id,
                "index_run_id": ch.index_run_id,
            })

    return results, run_ids, query_vec
