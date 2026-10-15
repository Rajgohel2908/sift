"""
routers/rag.py — RAG Query and evaluation endpoints for Phase 4 baseline RAG.

POST /query          — Execute dense retrieval + LLM generation
GET  /queries/{id}   — Retrieve a previous query and its cited chunks
GET  /queries        — List recent queries
"""

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db, QueryRecord, RetrievalResult, Chunk
from generation.answer import execute_query

router = APIRouter()


class QueryRequest(BaseModel):
    question: str
    source_ids: List[int]
    index_run_id: Optional[int] = None
    top_k: Optional[int] = None
    min_score: Optional[float] = None


@router.post("/query", summary="Execute baseline RAG search and generation")
def query_endpoint(
    req: QueryRequest,
    db: Session = Depends(get_db),
):
    """
    Search indexed content and generate a grounded, cited answer.
    - If best score < min_score: returns status 'insufficient_evidence' (LLM is not called).
    - If answers lack valid citations: returns status 'unverified'.
    - If citations are valid: returns status 'answered'.
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    if not req.source_ids and req.index_run_id is None:
        raise HTTPException(status_code=400, detail="Must provide at least one source_id or index_run_id.")

    result = execute_query(
        db=db,
        question=req.question.strip(),
        source_ids=req.source_ids,
        index_run_id=req.index_run_id,
        top_k=req.top_k,
        min_score=req.min_score,
    )
    return result


@router.get("/queries/{query_id}", summary="Get query details and retrieval results")
def get_query_details(
    query_id: int,
    db: Session = Depends(get_db),
):
    """Retrieve details of a past query including retrieved chunks, scores, and citation status."""
    q = db.query(QueryRecord).filter(QueryRecord.id == query_id).first()
    if not q:
        raise HTTPException(status_code=404, detail=f"Query {query_id} not found.")

    results = (
        db.query(RetrievalResult)
        .filter(RetrievalResult.query_id == query_id)
        .order_by(RetrievalResult.rank.asc())
        .all()
    )

    retrieved_items = []
    for r in results:
        chunk = db.query(Chunk).filter(Chunk.id == r.chunk_id).first()
        retrieved_items.append({
            "rank": r.rank,
            "chunk_id": r.chunk_id,
            "score": r.score,
            "used_in_prompt": r.used_in_prompt,
            "cited": r.cited,
            "metadata": chunk.metadata_ if chunk else {},
            "snippet": (chunk.content[:150] + "...") if chunk and len(chunk.content) > 150 else (chunk.content if chunk else ""),
        })

    return {
        "id": q.id,
        "question": q.question,
        "source_ids": q.source_ids,
        "index_run_ids": q.index_run_ids,
        "top_k": q.top_k,
        "min_score": q.min_score,
        "status": q.status,
        "answer": q.answer,
        "latency_ms": q.latency_ms,
        "created_at": str(q.created_at),
        "retrieval_results": retrieved_items,
    }


@router.get("/queries", summary="List recent queries")
def list_queries(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """List recent search queries."""
    queries = (
        db.query(QueryRecord)
        .order_by(QueryRecord.id.desc())
        .limit(limit)
        .all()
    )

    return [
        {
            "id": q.id,
            "question": q.question,
            "source_ids": q.source_ids,
            "index_run_ids": q.index_run_ids,
            "top_k": q.top_k,
            "min_score": q.min_score,
            "status": q.status,
            "answer": q.answer,
            "created_at": str(q.created_at),
        }
        for q in queries
    ]
