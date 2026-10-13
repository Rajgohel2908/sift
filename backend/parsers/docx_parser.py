"""
parsers/docx_parser.py — Extracts text from a .docx file using python-docx.
Returns a dict with paragraph count and full text.
"""

from docx import Document


def parse_docx(filepath: str) -> dict:
    """
    Read every paragraph in the DOCX and join them with newlines.
    Returns:
        {
            "paragraph_count": int,
            "full_text": str
        }
    """
    doc = Document(filepath)
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]

    return {
        "paragraph_count": len(paragraphs),
        "full_text":        "\n".join(paragraphs),
    }
