"""
config.py — Configuration for Phase 4 Baseline RAG.

Reads configuration values from environment variables or .env file with safe defaults.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from backend directory if present
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
else:
    load_dotenv()

# Embedding configuration
EMBED_MODEL: str = os.getenv("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
# Column dimension fixed in chunks schema migration (384 for all-MiniLM-L6-v2)
EMBED_DIM_COLUMN: int = 384

# Chunking defaults
CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "800"))
CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "120"))

# Retrieval defaults
TOP_K: int = int(os.getenv("TOP_K", "5"))
MIN_SCORE: float = float(os.getenv("MIN_SCORE", "0.30"))

# LLM generation configuration
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "gemini")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
# GEMINI_MODEL must come from env, no hardcoded model name in generation code
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "30"))


def validate_embed_dim(actual_dim: int) -> None:
    """
    Validate that the embedding model dimension matches the database column dimension.
    Fails fast with a clear explanation if there is a mismatch.
    """
    if actual_dim != EMBED_DIM_COLUMN:
        raise ValueError(
            f"Embedding model dimension {actual_dim} does not match the chunks.embedding "
            f"column dimension ({EMBED_DIM_COLUMN}). A new database migration is required "
            f"to alter the chunks.embedding column dimension."
        )
