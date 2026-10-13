"""
parsers/pdf_parser.py — Extracts text from a PDF using PyMuPDF (fitz).
Returns a dict with page_count, full_text, and per-page breakdown.
"""

import fitz  # PyMuPDF


def parse_pdf(filepath: str) -> dict:
    """
    Open the PDF and extract text from every page.
    Returns:
        {
            "page_count": int,
            "full_text":  str,          # all pages joined
            "pages": [str, ...]         # per-page text
        }
    """
    doc = fitz.open(filepath)
    pages = []

    for page in doc:
        pages.append(page.get_text())   # plain text extraction

    doc.close()

    return {
        "page_count": len(pages),
        "full_text":  "\n".join(pages),
        "pages":      pages,
    }
