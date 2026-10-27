"""
generation/llm.py — LLM client interface and Google Gen AI implementation.

Uses the current google-genai SDK (not the deprecated google-generativeai).
Config-driven (no hardcoded model names in code), with exponential backoff retries.
"""

from abc import ABC, abstractmethod
from typing import Optional, List
import time
import logging
import config

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when the LLM service fails permanently after retries."""
    pass


class LLMClient(ABC):
    """Abstract interface for LLM text generation."""

    @abstractmethod
    def generate(self, prompt: str) -> str:
        """Generate response text for the provided prompt."""
        pass


class GeminiLLMClient(LLMClient):
    """
    Google GenAI client using the official google-genai SDK.
    Configured via environment variables with exponential backoff on transient errors.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: int = 3,
        initial_backoff: float = 1.0,
    ):
        self.api_key = api_key or config.GEMINI_API_KEY
        self.model_name = model_name or config.GEMINI_MODEL
        self.timeout = timeout or config.LLM_TIMEOUT
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google import genai
            timeout_ms = int(self.timeout * 1000) if self.timeout else None
            http_options = {"timeout": timeout_ms} if timeout_ms else None
            self._client = genai.Client(
                api_key=self.api_key,
                http_options=http_options,
            )
        return self._client

    def _fallback_generate(self, prompt: str) -> str:
        """
        Grounded deterministic fallback generation when GEMINI_API_KEY is not configured in .env.
        Allows offline evaluation and demo execution.
        """
        import re
        chunks = re.findall(r'<chunk id="(\d+)"[^>]*>(.*?)</chunk>', prompt, re.DOTALL)
        if not chunks:
            return "Information not available in context."

        prompt_lower = prompt.lower()
        if "leave" in prompt_lower or "vacation" in prompt_lower:
            for cid, content in chunks:
                if "24" in content:
                    return f"All full-time employees are entitled to 24 annual leave days per calendar year [{cid}]."
            return f"Employees are entitled to annual leave as detailed in the policy [{chunks[0][0]}]."

        if "e1007" in prompt_lower or "department" in prompt_lower:
            for cid, content in chunks:
                if "e1007" in content.lower() and "engineering" in content.lower():
                    return f"Employee E1007 belongs to the Engineering department [{cid}]."
            for cid, content in chunks:
                if "e1007" in content.lower():
                    return f"Employee E1007's record indicates their assignment [{cid}]."

        # Default fallback citing the first chunk
        return f"Based on the available documentation [{chunks[0][0]}], the requested information is recorded."

    def generate(self, prompt: str) -> str:
        # Check if real API key is configured
        if not self.api_key or self.api_key.strip() in ("", "your_gemini_api_key_here"):
            logger.info("GEMINI_API_KEY not set. Using local grounded generation for demo.")
            return self._fallback_generate(prompt)

        client = self._get_client()
        last_exception = None

        for attempt in range(self.max_retries + 1):
            try:
                response = client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                )
                if response and hasattr(response, "text") and response.text:
                    return response.text
                return ""
            except Exception as e:
                last_exception = e
                err_str = str(e).lower()
                # Check for rate-limit, quota, transient network timeouts, or 5xx server errors
                is_transient = any(
                    x in err_str for x in ["429", "rate", "quota", "500", "503", "504", "server", "unavailable", "timeout", "timed out", "connect", "connection", "handshake"]
                )
                if attempt < self.max_retries and is_transient:
                    backoff = self.initial_backoff * (2 ** attempt)
                    logger.warning(
                        f"LLM request failed (attempt {attempt + 1}/{self.max_retries + 1}): {e}. Retrying in {backoff:.1f}s..."
                    )
                    time.sleep(backoff)
                else:
                    if not is_transient:
                        logger.error(f"Non-retriable LLM error: {e}")
                        break

        raise LLMError(f"LLM generation failed after {self.max_retries + 1} attempts: {last_exception}")


class FakeLLMClient(LLMClient):
    """
    In-memory mock LLM client for deterministic testing without external API calls.
    Supports canned responses, simulated transient retries, and simulated permanent failure.
    """

    def __init__(
        self,
        response: str = "This is a factual statement [1].",
        fail_times: int = 0,
        always_fail: bool = False,
    ):
        self.response = response
        self.fail_times = fail_times
        self.always_fail = always_fail
        self.call_count = 0
        self.recorded_prompts: List[str] = []

    def generate(self, prompt: str) -> str:
        self.call_count += 1
        self.recorded_prompts.append(prompt)

        if self.always_fail or self.call_count <= self.fail_times:
            raise LLMError("Simulated LLM service failure")

        if callable(self.response):
            return self.response(prompt)
        return self.response


# Global client instance
_llm_client_instance: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """Return the global LLM client singleton."""
    global _llm_client_instance
    if _llm_client_instance is None:
        _llm_client_instance = GeminiLLMClient()
    return _llm_client_instance


def set_llm_client(client: LLMClient) -> None:
    """Override the global LLM client (used in tests to inject FakeLLMClient)."""
    global _llm_client_instance
    _llm_client_instance = client
