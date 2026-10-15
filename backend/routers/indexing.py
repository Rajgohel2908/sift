"""
routers/indexing.py — Indexing management endpoints for Phase 4 baseline RAG.

POST /sources/{id}/index         — Chunk, embed, and store active source content
GET  /sources/{id}/index-runs    — List all index runs for a source
GET  /index-runs/{id}/chunks     — Preview chunks with metadata (no embeddings)
"""

from typing import Optional, List
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

import config
from db import get_db, Source, Document, Record, SourceVersion, IndexRun, Chunk, safe_json
from indexing.chunker import chunk_active_source_content
from indexing.embedder import get_embedder

router = APIRouter()


class IndexRequest(BaseModel):
    version: Optional[int] = None
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None


@router.post("/sources/{source_id}/index", summary="Index active content for a source version")
def create_index_run(
    source_id: int,
    req: IndexRequest,
    db: Session = Depends(get_db),
):
    """
    Index active documents and structured records for a source into pgvector.
    - Only content with status='active' is chunked and embedded.
    - Re-indexing creates a new index run (older runs are preserved for comparison).
    - Fails fast if the embedding model dimension does not match the database column.
    """
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail=f"Source {source_id} not found.")

    # Determine version: user-specified or latest available version
    if req.version is not None:
        target_version = req.version
    else:
        latest_ver = (
            db.query(func.max(SourceVersion.version_no))
            .filter(SourceVersion.source_id == source_id)
            .scalar()
        )
        target_version = latest_ver if latest_ver is not None else 1

    chunk_size = req.chunk_size if req.chunk_size is not None else config.CHUNK_SIZE
    chunk_overlap = req.chunk_overlap if req.chunk_overlap is not None else config.CHUNK_OVERLAP

    embedder = get_embedder()
    # Dimension mismatch check fails fast with clear error
    config.validate_embed_dim(embedder.dimension)

    # 1. Create index_runs row in 'running' status
    run = IndexRun(
        source_id=source_id,
        source_version=target_version,
        embed_model=config.EMBED_MODEL,
        embed_dim=embedder.dimension,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        chunk_count=0,
        status="running",
        error=None,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        # 2. Fetch only ACTIVE documents and records for the target version
        docs = (
            db.query(Document)
            .filter(
                Document.source_id == source_id,
                Document.version_no == target_version,
                Document.status == "active",
            )
            .all()
        )

        recs = (
            db.query(Record)
            .filter(
                Record.source_id == source_id,
                Record.version_no == target_version,
                Record.status == "active",
            )
            .all()
        )

        # 3. Chunk active content
        chunks_data = chunk_active_source_content(
            source_id=source_id,
            source_name=source.name,
            source_type=source.source_type,
            source_version=target_version,
            documents=docs,
            records=recs,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

        # 4. Embed chunks in batches of 32
        if chunks_data:
            texts = [c["content"] for c in chunks_data]
            embeddings = embedder.embed_documents(texts)

            for c_data, emb in zip(chunks_data, embeddings):
                chunk_record = Chunk(
                    index_run_id=run.id,
                    source_id=source_id,
                    document_id=c_data["document_id"],
                    record_id=c_data["record_id"],
                    chunk_index=c_data["chunk_index"],
                    content=c_data["content"],
                    content_hash=c_data["content_hash"],
                    token_estimate=c_data["token_estimate"],
                    embedding=emb,
                    metadata_=safe_json(c_data["metadata"]),
                )
                db.add(chunk_record)

        # 5. Mark index run as completed
        run.chunk_count = len(chunks_data)
        run.status = "completed"
        run.finished_at = datetime.utcnow()
        db.commit()
        db.refresh(run)

        return {
            "id": run.id,
            "source_id": run.source_id,
            "source_version": run.source_version,
            "embed_model": run.embed_model,
            "embed_dim": run.embed_dim,
            "chunk_size": run.chunk_size,
            "chunk_overlap": run.chunk_overlap,
            "chunk_count": run.chunk_count,
            "status": run.status,
            "error": run.error,
            "created_at": str(run.created_at),
            "finished_at": str(run.finished_at) if run.finished_at else None,
        }

    except Exception as e:
        db.rollback()
        # Clean up any partial chunks for this failed run
        db.query(Chunk).filter(Chunk.index_run_id == run.id).delete()
        run.status = "failed"
        run.error = str(e)
        run.finished_at = datetime.utcnow()
        db.commit()
        raise HTTPException(status_code=500, detail=f"Indexing failed: {e}")


@router.get("/sources/{source_id}/index-runs", summary="List all index runs for a source")
def list_index_runs(source_id: int, db: Session = Depends(get_db)):
    """Return all index runs for a source ordered by most recent first."""
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail=f"Source {source_id} not found.")

    runs = (
        db.query(IndexRun)
        .filter(IndexRun.source_id == source_id)
        .order_by(IndexRun.id.desc())
        .all()
    )

    return [
        {
            "id": r.id,
            "source_id": r.source_id,
            "source_version": r.source_version,
            "embed_model": r.embed_model,
            "embed_dim": r.embed_dim,
            "chunk_size": r.chunk_size,
            "chunk_overlap": r.chunk_overlap,
            "chunk_count": r.chunk_count,
            "status": r.status,
            "error": r.error,
            "created_at": str(r.created_at),
            "finished_at": str(r.finished_at) if r.finished_at else None,
        }
        for r in runs
    ]


@router.get("/index-runs/{index_run_id}/chunks", summary="Preview chunks for an index run")
def get_index_run_chunks(
    index_run_id: int,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """
    Return content preview and metadata for chunks of an index run.
    Embedding vectors are intentionally excluded from responses.
    """
    run = db.query(IndexRun).filter(IndexRun.id == index_run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail=f"Index run {index_run_id} not found.")

    chunks = (
        db.query(Chunk)
        .filter(Chunk.index_run_id == index_run_id)
        .order_by(Chunk.chunk_index.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    total = db.query(func.count(Chunk.id)).filter(Chunk.index_run_id == index_run_id).scalar()

    return {
        "index_run_id": index_run_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "chunks": [
            {
                "id": c.id,
                "index_run_id": c.index_run_id,
                "source_id": c.source_id,
                "document_id": c.document_id,
                "record_id": c.record_id,
                "chunk_index": c.chunk_index,
                "content": c.content,
                "content_hash": c.content_hash,
                "token_estimate": c.token_estimate,
                "metadata": c.metadata_,
            }
            for c in chunks
        ],
    }
