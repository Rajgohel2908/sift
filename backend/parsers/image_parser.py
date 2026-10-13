"""
parsers/image_parser.py — OCR an image with Tesseract via pytesseract.
Returns extracted text, average confidence, and a modality flag.
No external LLM or vision API is used.
"""

import os
import pytesseract
from PIL import Image
from fastapi import HTTPException

# ── Tesseract path auto-detection ────────────────────────────────────────────
# On Windows, Tesseract installs to C:\Program Files\Tesseract-OCR\ by default.
# If it's not on PATH we try that standard location automatically so the user
# doesn't need to set any environment variables.
_WINDOWS_DEFAULT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.path.exists(_WINDOWS_DEFAULT):
    pytesseract.pytesseract.tesseract_cmd = _WINDOWS_DEFAULT


def _tesseract_available() -> bool:
    """Return True if the Tesseract binary can be found and executed."""
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def parse_image(filepath: str) -> dict:
    """
    Run Tesseract OCR on the image.

    Returns:
        {
            "text":       str,
            "modality":   "ocr" | "ocr_low_confidence",
            "confidence": float,   # average word-level confidence (0-100)
            "warnings":   [str]    # populated if confidence is low or text too short
        }

    Raises:
        HTTPException 503 if Tesseract engine is not installed.
    """
    # Guard: give a clean error instead of a cryptic 500 if Tesseract is missing
    if not _tesseract_available():
        raise HTTPException(
            status_code=503,
            detail=(
                "Tesseract OCR engine is not installed or not found. "
                "Install it from https://github.com/UB-Mannheim/tesseract/wiki "
                "and ensure it is at C:\\Program Files\\Tesseract-OCR\\tesseract.exe "
                "or on the system PATH."
            ),
        )

    img = Image.open(filepath)

    # image_to_data returns a TSV-like dict with per-word confidence scores
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    # Filter to words that Tesseract actually detected (conf != -1)
    confidences = [
        int(c) for c in data["conf"]
        if str(c).lstrip("-").isdigit() and int(c) != -1
    ]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    # Extract plain text (strip and join non-empty words)
    words = [w for w in data["text"] if w.strip()]
    full_text = " ".join(words)

    warnings = []

    # Decide modality based on confidence and text length
    if avg_confidence < 60 or len(full_text) < 20:
        modality = "ocr_low_confidence"
        warnings.append(
            f"Low OCR quality: avg confidence {avg_confidence:.1f}%, "
            f"extracted {len(full_text)} characters. "
            "Consider using a higher-resolution image."
        )
    else:
        modality = "ocr"

    return {
        "text":       full_text,
        "modality":   modality,
        "confidence": round(avg_confidence, 2),
        "warnings":   warnings,
    }
