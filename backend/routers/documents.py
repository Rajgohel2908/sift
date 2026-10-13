"""
routers/documents.py — GET /documents/{doc_id} endpoint.

Returns the full document including the content field.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from db import get_db, Document

router = APIRouter()


@router.get("/documents/{doc_id}", summary="Get a full document including content")
def get_document(doc_id: int, db: Session = Depends(get_db)):
    """
    Return a single document by ID, including its full text content.
    For a list without content, use GET /sources/{source_id}/documents.
    """
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found.")

    return {
        "id":                    doc.id,
        "source_id":             doc.source_id,
        "modality":              doc.modality,
        "title":                 doc.title,
        "url":                   doc.url,
        "page_number":           doc.page_number,
        "word_count":            doc.word_count,
        "content_hash":          doc.content_hash,
        "extraction_confidence": doc.extraction_confidence,
        "published_at":          str(doc.published_at) if doc.published_at else None,
        "fetched_at":            str(doc.fetched_at),
        "metadata":              doc.metadata_,
        "quality":               doc.quality,
        # Full content is only returned here, not in the list endpoint
        "content":               doc.content,
    }
