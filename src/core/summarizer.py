"""LLM-powered document summarization."""
import time
from typing import Optional


class Summarizer:
    """Generate document summaries using an LLM, with TPM rate limiting."""

    def __init__(self, llm_client, tpm_limit: int = 5):
        self.llm = llm_client
        self.tpm_limit = tpm_limit
        self._last_call_time = 0.0

    def _rate_limit(self):
        """Enforce TPM (tokens per minute) limit by sleeping between calls."""
        if self.tpm_limit <= 0:
            return
        min_interval = 60.0 / self.tpm_limit
        elapsed = time.monotonic() - self._last_call_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_call_time = time.monotonic()

    def summarize(self, title: str, body: str, max_input_chars: int = 2000) -> Optional[str]:
        """Generate a concise summary of a document."""
        if not self.llm:
            return None

        # Truncate body to avoid token overflow
        truncated = body[:max_input_chars]

        prompt = f"""Summarize the following document in 2-3 sentences. Focus on: what it is about, key topics, and document type (e.g., contract, report, invoice, speech).

Title: {title}

Content:
{truncated}

Summary:"""

        self._rate_limit()

        try:
            response = self.llm.chat(prompt, max_tokens=150)
            return response.strip()
        except Exception as e:
            print(f"[Summarizer] LLM call failed: {e}")
            return None

    def batch_summarize(self, documents: list[dict], indexer) -> int:
        """Summarize a batch of documents, updating the indexer."""
        count = 0
        for doc in documents:
            summary = self.summarize(doc["title"], doc["body"])
            if summary:
                indexer.update_summary(doc["id"], summary)
                count += 1
        return count
