import os
import time
import logging
import json
import random
from typing import Any, Dict, List, Optional

from openai import OpenAI, APIError

from prompt_templates import (
    ttd_prompt_tmpl3,
    ttd_info_extract_prompt_tmpl_v2,
)
from providers.provider_base import BaseProvider

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429} | set(range(500, 600))
DEFAULT_MAX_SUBMIT_RETRIES = 3
RETRYABLE_ERRNOS = {11, 104, 110}


class OpenAIProvider(BaseProvider):
    """OpenAI provider with resilient batch helpers for TTD processing.

    Implements both the legacy single-article interface (BaseProvider) and
    batch-oriented helpers used by the new processing pipeline.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        completion_window: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("Missing OpenAI API key")

        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.completion_window = completion_window or os.getenv(
            "OPENAI_BATCH_COMPLETION_WINDOW", "24h"
        )
        self.client = OpenAI(api_key=self.api_key)
        self.poll_interval = 10
        self.max_poll_attempts = 1440

        self._result_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._error_cache: Dict[str, List[Dict[str, Any]]] = {}

    # --- Legacy single-article API (BaseProvider) ---

    def classify_article(self, title: str, content: str) -> str:  # type: ignore[override]
        """Synchronously classify a single article as 'true' or 'false'."""

        prompt = ttd_prompt_tmpl3.format(
            title=title or "",
            article_text=(content or "")[:800],
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.01,
            max_tokens=2,
        )
        text = (response.choices[0].message.content or "").strip().lower()
        return text

    def extract_metrics(self, content: str) -> str:  # type: ignore[override]
        """Synchronously extract metrics JSON string for a single article."""

        prompt = ttd_info_extract_prompt_tmpl_v2.format(
            article_text=(content or "")[:800]
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.01,
            max_tokens=350,
            stop=["```"],
        )
        text = response.choices[0].message.content or ""
        return self._strip_stop_tokens(text)

    # --- Request Builders ---

    def build_batch_requests(self, kind: str, items: List[Dict[str, Any]]) -> str:
        requests: List[Dict[str, Any]] = []
        for item in items:
            if kind == "classification":
                prompt = ttd_prompt_tmpl3.format(
                    title=item.get("title", ""),
                    article_text=item.get("content", "")[:800],
                )
                body = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.01,
                    "max_tokens": 2,
                }
            else:
                prompt = ttd_info_extract_prompt_tmpl_v2.format(
                    article_text=item.get("content", "")[:800]
                )
                body = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.01,
                    "max_tokens": 350,
                    "stop": ["```"],
                }

            requests.append(
                {
                    "custom_id": f"{item['article_id']}-{kind}",
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": body,
                }
            )

        return self._format_jsonl(requests)

    # --- Submission & Polling ---

    def submit_batch(
        self,
        kind: str,
        requests_jsonl: str,
        max_retries: int = DEFAULT_MAX_SUBMIT_RETRIES,
        base_delay: float = 5.0,
        backoff_factor: float = 2.0,
        jitter: float = 3.0,
    ) -> str:
        attempt = 0
        while True:
            try:
                batch_file = self.client.files.create(
                    file=(
                        f"batch_{kind}.jsonl",
                        requests_jsonl,
                        "application/json",
                    ),
                    purpose="batch",
                )

                batch = self.client.batches.create(
                    input_file_id=batch_file.id,
                    endpoint="/v1/chat/completions",
                    completion_window=self.completion_window,
                )

                logger.info(
                    "Submitted %s batch %s (attempt %s)", kind, batch.id, attempt + 1
                )
                return batch.id

            except (APIError, TimeoutError, OSError) as exc:  # noqa: BLE001
                attempt += 1
                if attempt > max_retries or not self._should_retry(exc):
                    logger.error("Giving up on submitting %s batch: %s", kind, exc)
                    raise

                # Prefer server-provided Retry-After (for 429) when available,
                # otherwise fall back to exponential backoff with jitter.
                retry_delay = self._retry_delay_from_exc(exc)
                if retry_delay is not None:
                    delay = retry_delay
                else:
                    delay = self._backoff_delay(base_delay, backoff_factor, attempt, jitter)

                logger.warning(
                    "Transient error submitting %s batch (%s). Retrying in %.1fs",
                    kind,
                    exc,
                    delay,
                )
                time.sleep(delay)

    def poll_batch_status(
        self,
        batch_id: str,
        max_retries: int = DEFAULT_MAX_SUBMIT_RETRIES,
        base_delay: float = 5.0,
        backoff_factor: float = 2.0,
        jitter: float = 3.0,
    ) -> Dict[str, Any]:
        attempts = 0
        error_retries = 0

        while attempts < self.max_poll_attempts:
            try:
                batch = self.client.batches.retrieve(batch_id)
                logger.info(
                    "Batch %s status=%s (processed=%s fail=%s)",
                    batch_id,
                    batch.status,
                    batch.request_counts.completed,
                    batch.request_counts.failed,
                )

                if batch.status in {"completed", "failed", "expired"}:
                    return {
                        "status": batch.status,
                        "processed": batch.request_counts.completed,
                        "failed": batch.request_counts.failed,
                        "submitted_at": getattr(batch, "created_at", None),
                        "completed_at": getattr(batch, "completed_at", None),
                        "output_file_id": getattr(batch, "output_file_id", None),
                        "error_file_id": getattr(batch, "error_file_id", None),
                    }

                attempts += 1
                time.sleep(self.poll_interval)

            except (APIError, TimeoutError, OSError) as exc:  # noqa: BLE001
                error_retries += 1
                if not self._should_retry(exc) or error_retries > max_retries:
                    logger.error(
                        "Unrecoverable polling error for batch %s: %s", batch_id, exc
                    )
                    raise

                # Prefer server-provided Retry-After (for 429) when available,
                # otherwise fall back to exponential backoff with jitter.
                retry_delay = self._retry_delay_from_exc(exc)
                if retry_delay is not None:
                    delay = retry_delay
                else:
                    delay = self._backoff_delay(
                        base_delay, backoff_factor, error_retries, jitter
                    )

                logger.warning(
                    "Transient polling error for batch %s: %s. Sleeping %.1fs before retry",
                    batch_id,
                    exc,
                    delay,
                )
                time.sleep(delay)

        raise TimeoutError(f"Batch {batch_id} did not complete within timeout")

    # --- Results / Errors ---

    def get_batch_results(self, batch_id: str) -> List[Dict[str, Any]]:
        if batch_id in self._result_cache:
            return self._result_cache[batch_id]

        batch = self.client.batches.retrieve(batch_id)
        file_id = getattr(batch, "output_file_id", None)
        if not file_id:
            logger.warning("Batch %s has no output file", batch_id)
            return []

        lines = self._download_file(file_id)
        results = [json.loads(line) for line in lines if line]
        self._result_cache[batch_id] = results
        return results

    def get_batch_errors(self, batch_id: str) -> List[Dict[str, Any]]:
        if batch_id in self._error_cache:
            return self._error_cache[batch_id]

        batch = self.client.batches.retrieve(batch_id)
        file_id = getattr(batch, "error_file_id", None)
        if not file_id:
            logger.info("Batch %s has no error file", batch_id)
            return []

        lines = self._download_file(file_id)
        errors = [json.loads(line) for line in lines if line]
        self._error_cache[batch_id] = errors
        logger.warning("Batch %s reported %s errors", batch_id, len(errors))
        return errors

    def parse_response(self, result: Dict[str, Any], kind: str) -> Optional[Any]:
        raw = self._extract_response_text(result)
        if not raw:
            return None

        if kind == "classification":
            return raw.strip().lower()

        if kind == "extraction":
            stripped = self._strip_stop_tokens(raw)
            try:
                parsed = json.loads(stripped)
                return parsed
            except json.JSONDecodeError as exc:  # noqa: BLE001
                logger.error("Failed to parse JSON for extraction response: %s", exc)
                return None

        return raw

    # --- Helpers ---

    @staticmethod
    def _format_jsonl(data: List[Dict[str, Any]]) -> str:
        return "\n".join(json.dumps(item, ensure_ascii=False) for item in data)

    @staticmethod
    def _extract_response_text(result: Dict[str, Any]) -> Optional[str]:
        # The batch output schema may include an "error" field set to null even
        # for successful responses. Only treat it as an error when it is
        # truthy, otherwise fall through to normal response handling.
        if result.get("error"):
            logger.error("Batch result error: %s", result["error"])
            return None

        response = result.get("response", {})
        body = response.get("body", {})
        choices = body.get("choices", [])
        if not choices:
            logger.warning("No choices found in batch result")
            return None

        text = choices[0].get("message", {}).get("content", "")
        return text.strip()

    @staticmethod
    def _strip_stop_tokens(text: str) -> str:
        """Remove outer Markdown code fences without touching inner content."""

        if not text:
            return ""

        text = text.strip()

        # Strip leading ``` or ```json fences
        if text.startswith("```"):
            first_newline = text.find("\n")
            if first_newline != -1:
                text = text[first_newline + 1 :]
            else:
                # Single-line fenced block like "```{...}```"
                text = text.lstrip("`")

        # Strip trailing ``` fence
        if text.endswith("```"):
            text = text[: -3]

        return text.strip()

    def _download_file(self, file_id: str) -> List[str]:
        file_content = self.client.files.content(file_id)
        return file_content.text.strip().split("\n") if file_content.text else []

    @staticmethod
    def _backoff_delay(base: float, factor: float, attempt: int, jitter: float) -> float:
        delay = base * (factor ** max(0, attempt - 1))
        return delay + random.uniform(0, jitter)

    @staticmethod
    def _retry_delay_from_exc(exc: Exception) -> Optional[float]:
        """Return server-specified retry delay (seconds) for rate limits, if any.

        For 429 responses, OpenAI may include a Retry-After header indicating how
        long the client should wait before retrying. When present and parseable,
        we prefer that over our own backoff schedule.
        """

        if isinstance(exc, APIError):
            status = getattr(exc, "http_status", None)
            if status == 429:
                headers = getattr(exc, "headers", None) or {}
                retry_after = (
                    headers.get("Retry-After")
                    or headers.get("retry-after")
                )
                if retry_after is not None:
                    try:
                        return float(retry_after)
                    except (TypeError, ValueError):
                        # Fall back to exponential backoff when header is malformed.
                        return None
        return None

    @staticmethod
    def _should_retry(exc: Exception) -> bool:
        if isinstance(exc, APIError):
            status = getattr(exc, "http_status", None)
            return status in RETRYABLE_STATUS_CODES
        if isinstance(exc, TimeoutError):
            return True
        if isinstance(exc, OSError):
            errno = getattr(exc, "errno", None)
            return errno in RETRYABLE_ERRNOS
        return False
